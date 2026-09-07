#include "edge_analysis.h"
#include <ArduinoJson.h>
#include <algorithm>
#include <cmath>
#include <complex>
#include <cstring>
#include <vector>

namespace EdgeAnalysis {
namespace {
constexpr double FFT_PI = 3.14159265358979323846;
bool hexId(const char* value) {
    if(!value || std::strlen(value)!=32) return false;
    for(unsigned i=0;i<32;++i) if(!((value[i]>='0' && value[i]<='9') || (value[i]>='a' && value[i]<='f'))) return false;
    return true;
}
void fft(std::vector<std::complex<double>>& a) {
    const auto n=a.size();
    for (std::size_t i=1,j=0;i<n;++i) {
        auto bit=n>>1;
        for (;j&bit;bit>>=1) j^=bit;
        j^=bit; if(i<j) std::swap(a[i],a[j]);
    }
    for (std::size_t len=2;len<=n;len<<=1) {
        const auto step=std::polar(1.0,-2*FFT_PI/len);
        for (std::size_t i=0;i<n;i+=len) {
            std::complex<double> w(1,0);
            for (std::size_t j=0;j<len/2;++j) {
                const auto u=a[i+j], v=a[i+j+len/2]*w;
                a[i+j]=u+v; a[i+j+len/2]=u-v; w*=step;
            }
        }
    }
}
std::string encode(const std::vector<float>& samples) {
    static const char* alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string bytes;
    bytes.reserve(samples.size()*4);
    for(float sample:samples) {
        std::uint32_t bits; std::memcpy(&bits,&sample,4);
        for(unsigned shift=0;shift<32;shift+=8) bytes.push_back(static_cast<char>(bits>>shift));
    }
    std::string out; out.reserve((bytes.size()+2)/3*4);
    for(std::size_t i=0;i<bytes.size();i+=3) {
        std::uint32_t bits=static_cast<unsigned char>(bytes[i])<<16;
        if(i+1<bytes.size()) bits|=static_cast<unsigned char>(bytes[i+1])<<8;
        if(i+2<bytes.size()) bits|=static_cast<unsigned char>(bytes[i+2]);
        out.push_back(alphabet[(bits>>18)&63]); out.push_back(alphabet[(bits>>12)&63]);
        out.push_back(i+1<bytes.size()?alphabet[(bits>>6)&63]:'=');
        out.push_back(i+2<bytes.size()?alphabet[bits&63]:'=');
    }
    return out;
}
bool addChannel(JsonObject channels, const char* name, const std::vector<float>& samples,
                unsigned rate, unsigned block, const char* unit, bool raw) {
    const auto features=summarize(samples.data(),samples.size(),block);
    if(!features.valid) return false;
    auto row=channels[name].to<JsonObject>();
    row["sampleRateHz"]=rate; row["sampleCount"]=samples.size(); row["unit"]=unit;
    auto values=row["features"].to<JsonObject>();
    values["rms"]=features.rms; values["peak"]=features.peak;
    if(features.hasKurtosis) values["kurtosis"]=features.kurtosis; else values["kurtosis"]=nullptr;
    auto bands=values["bandEnergy"].to<JsonArray>(); for(double energy:features.bandEnergy) bands.add(energy);
    if(raw) row["samplesFloat32LE"]=encode(samples);
    return true;
}
}

void PendingRequest::clear() {
    id_.clear(); expiresAtEpoch_ = 0;
}

bool PendingRequest::load(const char* response, const char* device, const char* site, const char* asset) {
    clear(); // A malformed, empty or remapped response cannot retain old authority.
    if(!response || !device || !site || !asset) return false;
    JsonDocument doc;
    if(deserializeJson(doc,response,DeserializationOption::NestingLimit(3)) ||
        doc["deviceId"] != device || doc["siteId"] != site || doc["assetId"] != asset ||
        !doc["requestId"].is<const char*>() || !hexId(doc["requestId"].as<const char*>()) ||
        doc["expiresAtEpoch"].is<bool>() || !doc["expiresAtEpoch"].is<double>()) return false;
    const double expires = doc["expiresAtEpoch"].as<double>();
    if(!std::isfinite(expires) || expires < 1700000000.0 || expires > 4102444800.0) return false;
    id_ = doc["requestId"].as<const char*>(); expiresAtEpoch_ = expires;
    return true;
}

const char* PendingRequest::forCapture(std::uint64_t capturedEpoch, double nowEpoch) {
    if(id_.empty()) return nullptr;
    // Unknown/backward time fails closed. Check both the captured timestamp and
    // the current sub-second UTC time before constructing a new raw file.
    if(capturedEpoch < 1700000000ULL || !std::isfinite(nowEpoch) ||
        nowEpoch < static_cast<double>(capturedEpoch) || nowEpoch >= expiresAtEpoch_ ||
        static_cast<double>(capturedEpoch) >= expiresAtEpoch_) {
        clear(); return nullptr;
    }
    return id_.c_str();
}

Features summarize(const float* samples, std::size_t count, std::size_t block) {
    Features out;
    if(!samples || count<2 || count>16384 || block<2 || block>2048 || (block&(block-1)) || count%block) return out;
    double mean=0, square=0;
    for(std::size_t i=0;i<count;++i) {
        if(!std::isfinite(samples[i]) || std::abs(samples[i])>1e9) return out;
        mean+=samples[i]; square+=static_cast<double>(samples[i])*samples[i];
        out.peak=std::max(out.peak,std::abs(static_cast<double>(samples[i])));
    }
    mean/=count; out.rms=std::sqrt(square/count);
    double variance=0, fourth=0;
    for(std::size_t i=0;i<count;++i) {const double d=samples[i]-mean; variance+=d*d; fourth+=d*d*d*d;}
    variance/=count; fourth/=count;
    if(variance>0) {out.hasKurtosis=true; out.kurtosis=fourth/(variance*variance);}
    std::vector<std::complex<double>> spectrum(block);
    for(std::size_t offset=0;offset<count;offset+=block) {
        double average=0; for(std::size_t i=0;i<block;++i) average+=samples[offset+i]; average/=block;
        for(std::size_t i=0;i<block;++i) spectrum[i]={samples[offset+i]-average,0};
        fft(spectrum);
        for(std::size_t k=1;k<=block/2;++k) {
            const auto band=k<block/16?0:k<block/8?1:2;
            out.bandEnergy[band]+=std::norm(spectrum[k])*(k==block/2?1:2)/(block*block)/(count/block);
        }
    }
    out.valid=true; return out;
}

std::string frame(const char* telemetry, const double* x, const double* y, const double* z,
                  const std::int32_t* audio, const char* requestId) {
    if(!telemetry || !x || !y || !z || !audio) return {};
    JsonDocument source;
    if(deserializeJson(source,telemetry) || !source["timestamp"].is<const char*>() || !source["sequence"].is<std::uint32_t>()) return {};
    JsonDocument document;
    document["schemaVersion"]=1; document["featureVersion"]="edge-statistics-v1";
    for(const char* key:{"timestamp","sequence","deviceId","siteId","assetId"}) document[key]=source[key];
    if(requestId && *requestId && !hexId(requestId)) return {};
    const bool raw=hexId(requestId);
    if(raw) document["requestId"]=requestId; else document["requestId"]=nullptr;
    auto channels=document["channels"].to<JsonObject>();
    const double* axes[]={x,y,z}; const char* names[]={"vibrationX","vibrationY","vibrationZ"};
    for(unsigned axis=0;axis<3;++axis) {
        std::vector<float> samples(512);
        for(unsigned i=0;i<512;++i) samples[i]=static_cast<float>(axes[axis][i]);
        if(!addChannel(channels,names[axis],samples,800,512,"g",raw)) return {};
    }
    std::vector<float> samples(10240);
    for(unsigned i=0;i<10240;++i) samples[i]=static_cast<float>(audio[i]);
    if(!addChannel(channels,"acoustic",samples,16000,2048,"pcm24",raw)) return {};
    if(document.overflowed() || measureJson(document)>MAX_FRAME_BYTES) return {};
    std::string output; serializeJson(document,output); return output;
}

bool accepted(int status, const char* response, std::uint32_t sequence) {
    if((status!=200 && status!=201) || !response) return false;
    JsonDocument doc;
    return !deserializeJson(doc,response,DeserializationOption::NestingLimit(3)) &&
        doc["accepted"].is<bool>() && doc["accepted"].as<bool>() &&
        !doc["sequence"].is<bool>() && doc["sequence"].is<std::uint32_t>() && doc["sequence"].as<std::uint32_t>()==sequence &&
        doc["frameId"].is<const char*>() && hexId(doc["frameId"].as<const char*>());
}
std::uint32_t checksum(const char* bytes,std::size_t count) {
    std::uint32_t value=0xffffffffU;
    for(std::size_t i=0;i<count;++i) {
        value^=static_cast<unsigned char>(bytes[i]);
        for(unsigned bit=0;bit<8;++bit) value=(value>>1)^((value&1)?0xedb88320U:0);
    }
    return ~value;
}
}

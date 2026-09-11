#include "vibration_window.h"
#include <algorithm>
#include <cmath>

namespace VibrationWindow {
namespace {
constexpr double Pi = 3.14159265358979323846;
void fft(Workspace& w) {
    for (unsigned i=1,j=0; i<Samples; ++i) {
        unsigned bit=Samples>>1;
        for (; j&bit; bit>>=1) j^=bit;
        j^=bit;
        if (i<j) {std::swap(w.real[i],w.real[j]); std::swap(w.imag[i],w.imag[j]);}
    }
    for (unsigned width=2; width<=Samples; width<<=1) {
        double sr=std::cos(-2*Pi/width), si=std::sin(-2*Pi/width);
        for (unsigned start=0; start<Samples; start+=width) {
            double wr=1, wi=0;
            for (unsigned j=0; j<width/2; ++j) {
                unsigned a=start+j,b=a+width/2;
                double vr=w.real[b]*wr-w.imag[b]*wi, vi=w.real[b]*wi+w.imag[b]*wr;
                w.real[b]=w.real[a]-vr; w.imag[b]=w.imag[a]-vi;
                w.real[a]+=vr; w.imag[a]+=vi;
                double next=wr*sr-wi*si; wi=wr*si+wi*sr; wr=next;
            }
        }
    }
}
}
const char* qualityName(Quality quality) {
    switch (quality) {
    case Quality::Valid: return "valid";
    case Quality::FifoOverrun: return "fifo_overrun";
    case Quality::SensorUnavailable: return "sensor_unavailable";
    case Quality::SampleGap: return "sample_gap";
    case Quality::Clipped: return "clipped";
    case Quality::ConstantAxis: return "constant_axis";
    default: return "processing_overflow";
    }
}
Features extract(const Raw& raw, Workspace& w) {
    Features out;
    out.index=raw.index; out.startUs=raw.startUs; out.count=raw.count; out.quality=raw.quality;
    if (raw.count == 0) { out.quality=Quality::SampleGap; return out; }
    if (out.quality!=Quality::Valid) return out;
    if (raw.count!=Samples) {out.quality=Quality::SampleGap; return out;}
    for (unsigned axis=0; axis<3; ++axis) {
        double mean=0;
        for (unsigned i=0; i<Samples; ++i) {
            // Full-resolution +/-16g rail is 13-bit; conservatively flag rails.
            if (raw.xyz[i][axis]<=-4096 || raw.xyz[i][axis]>=4095) {
                out.quality=Quality::Clipped; return out;
            }
            mean+=raw.xyz[i][axis]*0.0039;
        }
        mean/=Samples;
        double variance=0, fourth=0, peak=0, taperEnergy=0;
        for (unsigned i=0; i<Samples; ++i) {
            double ac=raw.xyz[i][axis]*0.0039-mean;
            double sq=ac*ac;
            variance+=sq; fourth+=sq*sq; peak=std::max(peak,std::abs(ac));
            // Periodic Hann, NOT symmetric Hann or the legacy Hamming FFT.
            double taper=0.5-0.5*std::cos(2*Pi*i/Samples);
            taperEnergy+=taper*taper; w.real[i]=ac*taper; w.imag[i]=0;
        }
        variance/=Samples; fourth/=Samples;
        if (variance<=1e-16) {out.quality=Quality::ConstantAxis; return out;}
        double* values=out.values+axis*7;
        values[0]=std::sqrt(variance); values[1]=peak; values[2]=fourth/(variance*variance);
        fft(w);
        double maxPower=-1;
        for (unsigned bin=1; bin<=Samples/2; ++bin) {
            double frequency=static_cast<double>(bin)*Rate/Samples;
            double power=(w.real[bin]*w.real[bin]+w.imag[bin]*w.imag[bin]) /
                         (Samples*taperEnergy)*(bin==Samples/2?1:2);
            if (power>maxPower) {maxPower=power; out.peakHz[axis]=frequency;}
            if (frequency<350) {
                unsigned band=frequency<50?0:frequency<100?1:frequency<200?2:3;
                values[3+band]+=power;
            }
        }
    }
    return out;
}
}

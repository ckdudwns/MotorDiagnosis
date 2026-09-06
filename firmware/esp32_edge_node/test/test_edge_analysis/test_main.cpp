#include "edge_analysis.h"
#include <ArduinoJson.h>
#include <unity.h>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <vector>

using namespace EdgeAnalysis;
constexpr double TAU=6.28318530717958647692;
const char* REQUEST="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
void setUp() {}
void tearDown() {}
void testSineBandsAndPearsonDefinition() {
    std::vector<float> samples(512);
    for(unsigned band=0;band<3;++band) {
        const unsigned bin=band==0?16:band==1?32:64;
        for(unsigned i=0;i<512;++i) samples[i]=2*std::sin(TAU*bin*i/512);
        auto result=summarize(samples.data(),512,512);
        TEST_ASSERT_TRUE(result.valid); TEST_ASSERT_TRUE(result.hasKurtosis);
        TEST_ASSERT_DOUBLE_WITHIN(1e-5,std::sqrt(2.0),result.rms);
        TEST_ASSERT_DOUBLE_WITHIN(1e-5,2,result.peak);
        TEST_ASSERT_DOUBLE_WITHIN(1e-5,1.5,result.kurtosis);
        for(unsigned j=0;j<3;++j) TEST_ASSERT_DOUBLE_WITHIN(1e-5,band==j?2:0,result.bandEnergy[j]);
    }
}
void testDcConstantAndNyquist() {
    std::vector<float> values(512,3);
    auto constant=summarize(values.data(),512,512);
    TEST_ASSERT_TRUE(constant.valid); TEST_ASSERT_FALSE(constant.hasKurtosis);
    TEST_ASSERT_EQUAL_DOUBLE(3,constant.rms);
    for(double band:constant.bandEnergy) TEST_ASSERT_EQUAL_DOUBLE(0,band);
    for(unsigned i=0;i<512;++i) values[i]=(i%2)?1:-1;
    auto nyquist=summarize(values.data(),512,512);
    TEST_ASSERT_DOUBLE_WITHIN(1e-8,1,nyquist.bandEnergy[2]);
}
void testInvalidSamplesAndBlockSize() {
    std::vector<float> values(512,1);
    TEST_ASSERT_FALSE(summarize(nullptr,512,512).valid);
    TEST_ASSERT_FALSE(summarize(values.data(),512,100).valid);
    TEST_ASSERT_FALSE(summarize(values.data(),511,512).valid);
    values[1]=std::numeric_limits<float>::quiet_NaN();
    TEST_ASSERT_FALSE(summarize(values.data(),512,512).valid);
    values[1]=std::numeric_limits<float>::infinity();
    TEST_ASSERT_FALSE(summarize(values.data(),512,512).valid);
}
std::string fixture(const char* timestamp, const char* request) {
    std::vector<double> x(512),y(512),z(512);
    std::vector<std::int32_t> audio(10240);
    for(unsigned i=0;i<512;++i) {x[i]=std::sin(TAU*16*i/512);y[i]=0.1;z[i]=(i%2)?1:-1;}
    for(unsigned i=0;i<10240;++i) audio[i]=static_cast<std::int32_t>(100000*std::sin(TAU*32*i/2048));
    JsonDocument packet;
    packet["timestamp"]=timestamp;packet["sequence"]=1;
    packet["siteId"]="SITE-01";packet["assetId"]="SITE-01-GEN-01";packet["deviceId"]="DEV-01-GEN-01";
    std::string source;serializeJson(packet,source);
    return frame(source.c_str(),x.data(),y.data(),z.data(),audio.data(),request);
}
void testSeparateRawEnvelopeAndFeatureOnlyEnvelope() {
    JsonDocument result;
    const auto raw=fixture("2026-09-07T00:00:00Z",REQUEST);
    TEST_ASSERT_TRUE(raw.size()<MAX_FRAME_BYTES);
    TEST_ASSERT_FALSE(deserializeJson(result,raw));
    TEST_ASSERT_EQUAL(1,result["schemaVersion"]);
    TEST_ASSERT_TRUE(result["rpm"].isNull());
    TEST_ASSERT_EQUAL(10240,result["channels"]["acoustic"]["sampleCount"]);
    TEST_ASSERT_EQUAL_STRING("pcm24",result["channels"]["acoustic"]["unit"]);
    TEST_ASSERT_EQUAL(54616,std::strlen(result["channels"]["acoustic"]["samplesFloat32LE"]));
    TEST_ASSERT_EQUAL_STRING(REQUEST,result["requestId"]);
    TEST_ASSERT_FALSE(deserializeJson(result,fixture("2026-09-07T00:00:00Z",nullptr)));
    TEST_ASSERT_TRUE(result["requestId"].isNull());
    TEST_ASSERT_TRUE(result["channels"]["acoustic"]["samplesFloat32LE"].isNull());
    TEST_ASSERT_TRUE(fixture("2026-09-07T00:00:00Z","invalid-request").empty());
}
void testAckFencesAndChecksum() {
    const char* ack="{\"accepted\":true,\"sequence\":1,\"frameId\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}";
    TEST_ASSERT_TRUE(accepted(201,ack,1));TEST_ASSERT_TRUE(accepted(200,ack,1));
    TEST_ASSERT_FALSE(accepted(500,ack,1));TEST_ASSERT_FALSE(accepted(200,ack,2));
    TEST_ASSERT_FALSE(accepted(200,"{}",1));
    TEST_ASSERT_FALSE(accepted(200,"{\"accepted\":true,\"sequence\":1,\"frameId\":\"zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz\"}",1));
    TEST_ASSERT_EQUAL_HEX32(0xcbf43926,checksum("123456789",9));
    TEST_ASSERT_NOT_EQUAL(checksum("original",8),checksum("changed!",8));
}
int main(int argc,char** argv) {
    if(argc==3 && std::strcmp(argv[1],"--emit-fixture")==0) {std::puts(fixture(argv[2],REQUEST).c_str());return 0;}
    UNITY_BEGIN();
    RUN_TEST(testSineBandsAndPearsonDefinition);
    RUN_TEST(testDcConstantAndNyquist);
    RUN_TEST(testInvalidSamplesAndBlockSize);
    RUN_TEST(testSeparateRawEnvelopeAndFeatureOnlyEnvelope);
    RUN_TEST(testAckFencesAndChecksum);
    return UNITY_END();
}

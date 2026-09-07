#include "vibration_window.h"
#include <unity.h>
#include <cmath>
#include <cstdio>
#include <cstring>

using namespace VibrationWindow;
Workspace workspace;
Raw raw;
void setUp() {raw=Raw(); raw.count=Samples;}
void tearDown() {}
void sine() {
    for (unsigned i=0;i<Samples;++i)
        for (unsigned a=0;a<3;++a)
            raw.xyz[i][a]=static_cast<std::int16_t>(std::lround(
                (a+1)*31*std::sin(6.283185307179586*(16+48*a)*i/Samples)+100*(a+1)));
}
void testAcMeanRemovalAndPearson() {
    sine();
    auto values=extract(raw,workspace);
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Quality::Valid),static_cast<int>(values.quality));
    for (unsigned a=0;a<3;++a) {
        TEST_ASSERT_DOUBLE_WITHIN(0.002,31*(a+1)*0.0039/std::sqrt(2.0),values.values[a*7]);
        TEST_ASSERT_DOUBLE_WITHIN(0.03,1.5,values.values[a*7+2]);
    }
}
void testInvalidNeverProducesValidZeroVector() {
    auto constant=extract(raw,workspace);
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Quality::ConstantAxis),static_cast<int>(constant.quality));
    sine(); raw.xyz[0][0]=4095;
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Quality::Clipped),static_cast<int>(extract(raw,workspace).quality));
    raw.count=511;
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Quality::SampleGap),static_cast<int>(extract(raw,workspace).quality));
    raw.quality=Quality::FifoOverrun;
    TEST_ASSERT_EQUAL_STRING("fifo_overrun",qualityName(extract(raw,workspace).quality));
}
void testBoundedQueueKeepsInflightHead() {
    Queue<unsigned,3> q;
    TEST_ASSERT_TRUE(q.push(0)); TEST_ASSERT_TRUE(q.push(1)); TEST_ASSERT_TRUE(q.push(2));
    TEST_ASSERT_FALSE(q.push(3)); TEST_ASSERT_EQUAL_UINT(1,q.dropped);
    TEST_ASSERT_EQUAL_UINT(0,q.at(0));
    TEST_ASSERT_FALSE(q.acknowledge(4)); TEST_ASSERT_EQUAL_UINT(3,q.size());
    TEST_ASSERT_TRUE(q.acknowledge(2)); TEST_ASSERT_TRUE(q.push(4));
    TEST_ASSERT_EQUAL_UINT(2,q.at(0)); TEST_ASSERT_EQUAL_UINT(4,q.at(1));
}
void testEveryWindowSurvivesSlowBatchConsumer() {
    Queue<unsigned,64> q;
    unsigned acknowledged=0;
    for (unsigned i=0;i<1000;++i) {
        TEST_ASSERT_TRUE(q.push(i));
        if (i%8==7) {
            for(unsigned j=0;j<8;++j) TEST_ASSERT_EQUAL_UINT(acknowledged++,q.at(j));
            TEST_ASSERT_TRUE(q.acknowledge(8));
        }
    }
    TEST_ASSERT_EQUAL_UINT(1000,acknowledged); TEST_ASSERT_EQUAL_UINT(0,q.dropped);
}
void testRawTransportByteOrderAndBounds() {
    raw.count=1; raw.xyz[0][0]=-4096; raw.xyz[0][1]=4095; raw.xyz[0][2]=-1;
    std::uint8_t out[6]{};
    const std::uint8_t expected[6]={0,0xf0,0xff,0x0f,0xff,0xff};
    TEST_ASSERT_TRUE(serializeCountsLE(raw,out,sizeof(out)));
    TEST_ASSERT_EQUAL_MEMORY(expected,out,sizeof(out));
    TEST_ASSERT_FALSE(serializeCountsLE(raw,out,5));
    raw.count=513; TEST_ASSERT_FALSE(serializeCountsLE(raw,out,sizeof(out)));
}
void testRawQueueOwnsSamplesAndKeepsInflightHead() {
    Queue<Raw,8> q;
    sine();
    for(unsigned i=0;i<8;++i) {raw.index=i; TEST_ASSERT_TRUE(q.push(raw));}
    const auto first=q.at(0).xyz[0][0];
    raw.xyz[0][0]=1234; raw.index=8;
    TEST_ASSERT_FALSE(q.push(raw));
    TEST_ASSERT_EQUAL_INT(first,q.at(0).xyz[0][0]);
    TEST_ASSERT_EQUAL_UINT(0,q.at(0).index);
    TEST_ASSERT_TRUE(q.acknowledge(2)); TEST_ASSERT_TRUE(q.push(raw));
    TEST_ASSERT_EQUAL_UINT(2,q.at(0).index);
    TEST_ASSERT_EQUAL_INT(1234,q.at(6).xyz[0][0]);
}
int main(int argc,char** argv) {
    if(argc==2 && std::strcmp(argv[1],"--emit-fixture")==0) {
        setUp(); sine(); auto f=extract(raw,workspace);
        std::printf("{\"counts\":[");
        for(unsigned i=0;i<Samples;++i)
            std::printf("%s[%d,%d,%d]",i?",":"",raw.xyz[i][0],raw.xyz[i][1],raw.xyz[i][2]);
        std::printf("],\"features\":[");
        for(unsigned i=0;i<FeatureCount;++i) std::printf("%s%.17g",i?",":"",f.values[i]);
        std::printf("]}\n"); return 0;
    }
    UNITY_BEGIN();
    RUN_TEST(testAcMeanRemovalAndPearson);
    RUN_TEST(testInvalidNeverProducesValidZeroVector);
    RUN_TEST(testBoundedQueueKeepsInflightHead);
    RUN_TEST(testEveryWindowSurvivesSlowBatchConsumer);
    RUN_TEST(testRawTransportByteOrderAndBounds);
    RUN_TEST(testRawQueueOwnsSamplesAndKeepsInflightHead);
    return UNITY_END();
}

#include "adaptive_transmission.h"
#include "adaptive_transmission_ack.h"
#include <unity.h>
#include <limits>
using namespace AdaptiveTransmission;
using namespace VibrationWindow;
void setUp() {}
void tearDown() {}
Config config() {Config c; c.low=.7; c.high=1.3; c.severeLow=.3; c.severeHigh=2; c.severePeak=3; return c;}
Features frame(unsigned i,double rms=1,Quality q=Quality::Valid) {
    Features f; f.index=i; f.startUs=std::uint64_t(i)*655000; f.count=512; f.quality=q;
    f.values[0]=rms; f.values[1]=rms*1.1; return f;
}
void testBaselineMissingNeverEnablesFiveMinuteBlindWait() {
    Detector d; TEST_ASSERT_FALSE(d.configured());
    TEST_ASSERT_EQUAL_INT(int(Reason::BaselineMissing),int(d.update(frame(0)))); TEST_ASSERT_TRUE(d.active());
}
void testTwoOfThreeAndSingleDeviation() {
    Detector d(config()); d.update(frame(0,1.4)); TEST_ASSERT_FALSE(d.active());
    d.update(frame(1)); TEST_ASSERT_FALSE(d.active());
    TEST_ASSERT_EQUAL_INT(int(Reason::High),int(d.update(frame(2,1.4)))); TEST_ASSERT_TRUE(d.active());
    Detector low(config()); low.update(frame(0,.6)); low.update(frame(1,.6));
    TEST_ASSERT_EQUAL_INT(int(Reason::Low),int(low.reason()));
}
void testSevereAndQualityAreImmediateNotModelVerdicts() {
    Detector d(config()); TEST_ASSERT_EQUAL_INT(int(Reason::Severe),int(d.update(frame(0,.1))));
    TEST_ASSERT_EQUAL_INT(int(Reason::Quality),int(d.update(frame(1,1,Quality::FifoOverrun))));
    auto f=frame(2); f.values[0]=std::numeric_limits<double>::quiet_NaN();
    TEST_ASSERT_EQUAL_INT(int(Reason::Quality),int(d.update(f)));
}
void testGapQualityAndRebootResetVoting() {
    Detector d(config()); d.update(frame(0,1.4)); d.update(frame(3,1.4));
    TEST_ASSERT_EQUAL_INT(int(Reason::Quality),int(d.reason()));
    Detector reboot(config()); reboot.update(frame(5,1.4)); reboot.update(frame(0,1.4));
    TEST_ASSERT_EQUAL_INT(int(Reason::Quality),int(reboot.reason()));
    Detector time(config()); time.update(frame(0,1.4)); auto f=frame(1,1.4); f.startUs=2000;
    time.update(f); TEST_ASSERT_EQUAL_INT(int(Reason::Quality),int(time.reason()));
}
void testRecoveryRequiresSustainedValidNormalWindows() {
    Detector d(config()); d.update(frame(0,2.1));
    for (unsigned i=1;i<47;++i) {d.update(frame(i)); TEST_ASSERT_TRUE(d.active());}
    d.update(frame(47)); TEST_ASSERT_FALSE(d.active());
    d.update(frame(48,2.1)); d.update(frame(49)); d.update(frame(100));
    TEST_ASSERT_TRUE(d.active());
}
void testCrcDetectsTornPayload() {
    char bytes[]="123456789"; TEST_ASSERT_EQUAL_HEX32(0xcbf43926U,crc32(bytes,9));
    auto before=crc32(bytes,9); bytes[4]^=1; TEST_ASSERT_NOT_EQUAL(before,crc32(bytes,9));
}
void testFiveMinuteSnapshotAndPriorityPreemption() {
    FlushSchedule s;
    s.poll(299999,468); TEST_ASSERT_EQUAL_UINT64(0,s.cutoff());
    s.poll(300000,469); TEST_ASSERT_EQUAL_UINT64(469,s.cutoff());
    s.poll(310000,484); TEST_ASSERT_EQUAL_UINT64(469,s.cutoff());
    Slot slots[3]={{1,0,true,false,false},{469,468,true,true,false},{470,469,true,true,true}};
    TEST_ASSERT_EQUAL_UINT(2,selectSlot(slots,3,true,0,s.cutoff()));
    TEST_ASSERT_EQUAL_UINT(0,selectSlot(slots,3,false,0,s.cutoff()));
    TEST_ASSERT_EQUAL_UINT(1,selectSlot(slots,3,false,1,s.cutoff()));
    TEST_ASSERT_EQUAL_UINT(3,selectSlot(slots,3,false,469,s.cutoff()));
    // A failed HTTP/ACK never mutates slots: the next selection is identical.
    TEST_ASSERT_EQUAL_UINT(2,selectSlot(slots,3,true,0,s.cutoff()));
    s.drained(); s.poll(599999,938); TEST_ASSERT_EQUAL_UINT64(0,s.cutoff());
    s.poll(600000,938); TEST_ASSERT_EQUAL_UINT64(938,s.cutoff());
}
void testPressureTimerWrapAndUnresolvedOldBoot() {
    FlushSchedule s(0xfffffff0U); s.poll(15,1); TEST_ASSERT_EQUAL_UINT64(0,s.cutoff());
    s.poll(20,1,true); TEST_ASSERT_EQUAL_UINT64(1,s.cutoff());
    Slot slots[2]={{1,0,true,false,false,false},{2,0,true,true,false,true}};
    TEST_ASSERT_EQUAL_UINT(1,selectSlot(slots,2,false,0,2));
}
void testAckRequiresEveryImmutableIdentityAndSuccessfulStorageResponse() {
    struct Record { const char* device="DEV-01-MOT-02"; const char* bootId="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"; Raw raw; } records[2];
    records[0].raw.index=2; records[1].raw.index=3;
    JsonDocument doc;
    doc["deviceId"]=records[0].device; doc["accepted"]=2;
    auto ack=doc["acknowledged"].to<JsonArray>();
    for (unsigned i=0;i<2;++i) {
        auto entry=ack.add<JsonObject>(); entry["bootId"]=records[i].bootId;
        entry["windowIndex"]=records[i].raw.index;
        entry["digest"]="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    }
    auto ok=[&](int status){return storageAck(status,doc.as<JsonVariantConst>(),records,2);};
    TEST_ASSERT_TRUE(ok(202)); TEST_ASSERT_FALSE(ok(201)); TEST_ASSERT_FALSE(ok(200));
    doc["accepted"]=0; TEST_ASSERT_TRUE(ok(200)); TEST_ASSERT_FALSE(ok(202));
    doc["accepted"]=true; TEST_ASSERT_FALSE(ok(200)); doc["accepted"]=0;
    ack[1]["windowIndex"]=4; TEST_ASSERT_FALSE(ok(200)); ack[1]["windowIndex"]=3;
    ack[0]["bootId"]="other"; TEST_ASSERT_FALSE(ok(200)); ack[0]["bootId"]=records[0].bootId;
    ack[0]["digest"]="short"; TEST_ASSERT_FALSE(ok(200));
}
int main() {
    UNITY_BEGIN();
    RUN_TEST(testBaselineMissingNeverEnablesFiveMinuteBlindWait);
    RUN_TEST(testTwoOfThreeAndSingleDeviation);
    RUN_TEST(testSevereAndQualityAreImmediateNotModelVerdicts);
    RUN_TEST(testGapQualityAndRebootResetVoting);
    RUN_TEST(testRecoveryRequiresSustainedValidNormalWindows);
    RUN_TEST(testCrcDetectsTornPayload);
    RUN_TEST(testFiveMinuteSnapshotAndPriorityPreemption);
    RUN_TEST(testPressureTimerWrapAndUnresolvedOldBoot);
    RUN_TEST(testAckRequiresEveryImmutableIdentityAndSuccessfulStorageResponse);
    return UNITY_END();
}

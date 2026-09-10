// Included inside ContinuousVibration after timestamp(). Only the spool task
// writes raw files; network reads/deletes under fsMutex. No flash in FIFO task.
namespace AdaptiveRuntime {
using AdaptiveTransmission::Reason;
constexpr unsigned Slots=768;
constexpr std::uint32_t Magic=0x31574241;
struct Record {
    std::uint32_t magic=Magic, version=1;
    std::uint64_t order=0;
    char bootId[33]{}, device[129]{}, site[129]{}, asset[129]{}, baseline[65]{};
    Raw raw;
    Reason reason=Reason::None;
    std::uint32_t crc=0;
};
static_assert(sizeof(Record)<4096,"One record must fit the reserved filesystem budget");
using Slot=AdaptiveTransmission::Slot;
Slot slots[Slots];
struct Frame {Raw raw; Reason reason; bool activated;};
QueueHandle_t spoolQueue=nullptr;
SemaphoreHandle_t fsMutex=nullptr;
AdaptiveTransmission::Detector detector(adaptiveConfig());
std::atomic<bool> urgent{true}, storageFault{false};
std::atomic<std::uint32_t> drops{0}, pendingCount{0};
std::uint64_t nextOrder=1;
std::int64_t currentAnchor=0;
Record netBatch[AdaptiveTransmission::BatchSize];
unsigned selected[AdaptiveTransmission::BatchSize];
String path(unsigned slot) {return String("/raw-spool/")+slot+".bin";}
std::uint32_t checksum(const Record& r) {return AdaptiveTransmission::crc32(&r,offsetof(Record,crc));}
bool valid(const Record& r) {
    return r.magic==Magic && r.version==1 && r.order && r.raw.count<=512 &&
        r.bootId[32]==0 && strlen(r.bootId)==32 && r.device[128]==0 && r.site[128]==0 &&
        r.asset[128]==0 && r.baseline[64]==0 && static_cast<unsigned>(r.reason)<=7 && r.crc==checksum(r);
}
bool read(const String& name, Record& r) {
    File f=LittleFS.open(name,"r");
    if (!f) return false;
    bool ok=f.size()==sizeof(r) && f.read(reinterpret_cast<uint8_t*>(&r),sizeof(r))==sizeof(r);
    f.close(); return ok && valid(r);
}
// A boot-specific UTC anchor survives reboot. Never use a new boot's UTC for
// older unresolved windows. Files are small and immutable after creation.
struct Anchor {std::int64_t offset=0; std::uint32_t crc=0;};
bool loadAnchor(const char* id, std::int64_t& offset) {
    File f=LittleFS.open(String("/raw-spool/a-")+id,"r");
    if (!f) return false;
    Anchor a{};
    bool ok=f.size()==sizeof(a) && f.read(reinterpret_cast<uint8_t*>(&a),sizeof(a))==sizeof(a);
    f.close();
    if (!ok || a.offset<=0 || a.crc!=AdaptiveTransmission::crc32(&a.offset,sizeof(a.offset))) return false;
    offset=a.offset; return true;
}
bool anchorCurrentBoot() {
    if (currentAnchor) return true;
    if (loadAnchor(boot,currentAnchor)) return true;
    timeval tv{}; gettimeofday(&tv,nullptr);
    if (tv.tv_sec<1700000000) return false;
    Anchor a{}; a.offset=std::int64_t(tv.tv_sec)*1000000+tv.tv_usec-esp_timer_get_time();
    a.crc=AdaptiveTransmission::crc32(&a.offset,sizeof(a.offset));
    String final=String("/raw-spool/a-")+boot, temp=final+".tmp";
    File f=LittleFS.open(temp,"w");
    if (!f) return false;
    bool ok=f.write(reinterpret_cast<const uint8_t*>(&a),sizeof(a))==sizeof(a);
    f.flush(); f.close();
    if (!ok || !LittleFS.rename(temp,final)) return false;
    return loadAnchor(boot,currentAnchor);
}
bool initialize() {
    if (strlen(DEVICE_ID)>128 || strlen(SITE_ID)>128 || strlen(ASSET_ID)>128) return false;
    // Never format or repartition automatically. Legacy N8's 1.5 MiB FS is not enough.
    if (LittleFS.totalBytes()<4U*1024*1024) {
        Serial.println("[ADAPTIVE] Need enlarged filesystem; existing files preserved. See migration proposal.");
        return false;
    }
    fsMutex=xSemaphoreCreateMutex(); spoolQueue=xQueueCreate(4,sizeof(Frame));
    if (!fsMutex || !spoolQueue || (!LittleFS.exists("/raw-spool") && !LittleFS.mkdir("/raw-spool"))) return false;
    Record restored;
    unsigned used=0;
    for (unsigned i=0;i<Slots;++i) {
        const String final=path(i), temp=final+".tmp";
        if (LittleFS.exists(temp)) {
            // Recover a fully verified pre-rename write. Partial files are kept
            // for inspection; refusing startup avoids silently discarding data.
            if (LittleFS.exists(final) || !read(temp,restored) || !LittleFS.rename(temp,final)) return false;
        }
        if (!LittleFS.exists(final)) continue;
        if (!read(final,restored)) return false;
        std::int64_t offset=0;
        const bool resolved=loadAnchor(restored.bootId,offset);
        slots[i]={restored.order,restored.raw.index,true,false,false,resolved};
        if (!resolved) Serial.printf("[ADAPTIVE] Preserved unresolved UTC: boot=%s index=%lu\n",restored.bootId,static_cast<unsigned long>(restored.raw.index));
        if (restored.order>=nextOrder) nextOrder=restored.order+1;
        ++used;
    }
    pendingCount.store(used);
    Serial.printf("[ADAPTIVE] Restored %u raw windows; baseline=%s; %s\n",used,ADAPTIVE_BASELINE_ID,
                  detector.configured()?"5-minute batching enabled":"baseline missing: fast transfer retained");
    return true;
}
void submit(const Raw& raw, const Features& f) {
    const bool wasActive=detector.active();
    Reason reason=detector.update(f);
    Frame frame{raw,reason,!wasActive && detector.active()};
    urgent.store(detector.active());
    if (xQueueSend(spoolQueue,&frame,0)!=pdTRUE) {++drops; storageFault.store(true);}
}
bool append(const Frame& frame) {
    unsigned slot=0;
    while (slot<Slots && slots[slot].used) ++slot;
    if (slot==Slots || LittleFS.totalBytes()-LittleFS.usedBytes()<64*1024) return false;
    Record record{};
    record.order=nextOrder; record.raw=frame.raw; record.reason=frame.reason;
    strcpy(record.bootId,boot); strcpy(record.device,DEVICE_ID);
    strcpy(record.site,SITE_ID); strcpy(record.asset,ASSET_ID);
    strncpy(record.baseline,detector.configured()?ADAPTIVE_BASELINE_ID:"unconfigured",64); record.baseline[64]=0;
    record.crc=checksum(record);
    const String final=path(slot), temp=final+".tmp";
    if (LittleFS.exists(temp)) return false; // Preserve failed writes for recovery.
    File f=LittleFS.open(temp,"w");
    if (!f) return false;
    bool ok=f.write(reinterpret_cast<const uint8_t*>(&record),sizeof(record))==sizeof(record);
    f.flush(); f.close();
    Record verify;
    if (!ok || !read(temp,verify) || verify.crc!=record.crc || !LittleFS.rename(temp,final)) return false;
    slots[slot]={record.order,record.raw.index,true,true,frame.reason!=Reason::None};
    ++nextOrder; ++pendingCount;
    if (frame.reason!=Reason::None) {
        const auto first=record.raw.index>3?record.raw.index-3:0;
        for (auto& s:slots) if (s.used && s.current && s.index>=first) s.priority=true;
    }
    return true;
}
void spoolTask(void*) {
    Frame frame;
    bool held=false;
    while (true) {
        if (!held && xQueueReceive(spoolQueue,&frame,pdMS_TO_TICKS(100))!=pdTRUE) continue;
        held=true;
        xSemaphoreTake(fsMutex,portMAX_DELAY);
        anchorCurrentBoot();
        const bool saved=append(frame);
        xSemaphoreGive(fsMutex);
        if (saved) {held=false; storageFault.store(false);}
        else {storageFault.store(true); vTaskDelay(pdMS_TO_TICKS(200));}
    }
}
bool summaryDue() {
    static std::uint32_t last=millis();
    static bool draining=false;
    if (urgent.load() || storageFault.load() || healthJournal.count) return true;
    if (millis()-last>=AdaptiveTransmission::NormalIntervalMs) draining=true;
    if (draining && queueIsEmpty()) {draining=false; last=millis();}
    return draining;
}
void networkTask(void*) {
    AdaptiveTransmission::FlushSchedule schedule(millis());
    unsigned retryMs=200;
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(retryMs)); retryMs=200;
        if (WiFi.status()!=WL_CONNECTED) continue;
        xSemaphoreTake(fsMutex,portMAX_DELAY);
        anchorCurrentBoot();
        schedule.poll(millis(),nextOrder-1,pendingCount.load()>=600 || storageFault.load());
        unsigned size=0;
        bool priority=false;
        for (const auto& s:slots) if (s.used && s.priority) {priority=true; break;}
        // Select by persisted order, never directory iteration or arrival time.
        std::uint64_t after=0;
        for (;size<AdaptiveTransmission::BatchSize;++size) {
            const unsigned best=AdaptiveTransmission::selectSlot(slots,Slots,priority,after,schedule.cutoff());
            if (best==Slots) break;
            if (!read(path(best),netBatch[size])) {storageFault.store(true); break;}
            // One batch has one immutable baseline and measurement identity scope.
            if (size && (strcmp(netBatch[0].baseline,netBatch[size].baseline) ||
                         strcmp(netBatch[0].device,netBatch[size].device))) break;
            selected[size]=best; after=slots[best].order;
        }
        if (!priority && !size && schedule.cutoff()) schedule.drained();
        JsonDocument doc;
        bool ready=size>0;
        auto windows=doc["windows"].to<JsonArray>();
        for (unsigned i=0;ready && i<size;++i) {
            const auto& r=netBatch[i]; const auto& raw=r.raw;
            std::int64_t offset=0;
            if (!loadAnchor(r.bootId,offset)) {ready=false; storageFault.store(true); break;}
            auto w=windows.add<JsonObject>();
            w["schemaVersion"]=1; w["deviceId"]=r.device; w["siteId"]=r.site; w["assetId"]=r.asset;
            w["bootId"]=r.bootId; w["windowIndex"]=raw.index; w["startUptimeUs"]=raw.startUs;
            w["timestamp"]=timestamp(raw.startUs,offset); w["profileId"]="adxl345-800hz-xyz-counts-v1";
            w["sampleRateHz"]=800; w["sampleCount"]=raw.count; w["unit"]="count";
            w["quality"]=qualityName(raw.quality); w["encoding"]="base64-int16le-xyz"; w["gPerCount"]=0.0039;
            auto axes=w["axes"].to<JsonArray>(); axes.add("X"); axes.add("Y"); axes.add("Z");
            ready=encodeRaw(raw,w);
        }
        auto info=doc["transmission"].to<JsonObject>();
        info["policyId"]="edge-trigger-batch-v1";
        info["mode"]=priority?"priority":"periodic";
        // Context windows may precede the trigger; carry the actual trigger/hold
        // reason from the newest selected triggered window, not always item 0.
        Reason batchReason=Reason::None;
        for (unsigned i=0;i<size;++i) if (netBatch[i].reason!=Reason::None) batchReason=netBatch[i].reason;
        info["reason"]=priority?AdaptiveTransmission::reasonName(batchReason):"none";
        info["baselineId"]=size?netBatch[0].baseline:"unconfigured";
        info["droppedWindows"]=std::uint64_t(drops.load())+processingDrops.load();
        xSemaphoreGive(fsMutex);
        if (!ready || doc.overflowed()) continue;
        String body; serializeJson(doc,body);
        String ingest(INGEST_URL), suffix("/api/telemetry/ingest");
        if (!ingest.endsWith(suffix)) continue;
        String url=ingest.substring(0,ingest.length()-suffix.length())+"/api/devices/"+netBatch[0].device+"/raw-vibration-windows";
        WiFiClientSecure secure; BackendHttp http;
        secure.setHandshakeTimeout(3); http.setConnectTimeout(1500); http.setTimeout(1500);
        http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
        if (!beginBackendHttp(http,secure,url.c_str())) {retryMs=2000; continue;}
        http.addHeader("Authorization",String("Bearer ")+INGEST_TOKEN);
        http.addHeader("Content-Type","application/json");
        const int status=http.POST(body);
        bool accepted=false;
        if ((status==200 || status==202) && http.getSize()>=0 && http.getSize()<8192) {
            JsonDocument response;
            const String responseBody=http.getString();
            if (responseBody.length()==unsigned(http.getSize()) &&
                !deserializeJson(response,responseBody,DeserializationOption::NestingLimit(5)))
                accepted=AdaptiveTransmission::storageAck(status,response.as<JsonVariantConst>(),netBatch,size);
        }
        http.end();
        if (accepted) {
            xSemaphoreTake(fsMutex,portMAX_DELAY);
            for (unsigned i=0;i<size;++i) {
                auto& s=slots[selected[i]];
                if (s.used && s.order==netBatch[i].order && LittleFS.remove(path(selected[i]))) {
                    s=Slot{}; --pendingCount;
                } else storageFault.store(true); // Keep source on failed removal.
            }
            xSemaphoreGive(fsMutex);
        } else retryMs=2000;
        Serial.printf("[ADAPTIVE] HTTP %d ack=%u windows=%u mode=%s pending=%lu drops=%lu\n",
            status,accepted,size,priority?"priority":"periodic",
            static_cast<unsigned long>(pendingCount.load()),static_cast<unsigned long>(drops.load()+processingDrops.load()));
    }
}
} // namespace AdaptiveRuntime

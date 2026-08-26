#pragma once

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

#include <cJSON.h>
#include <esp_log.h>

#include "settings.h"

namespace moss::memory {

struct MemoryEntry {
    std::string key;
    std::string category;
    std::string value;
    uint32_t revision = 0;
};

// Small, explicit, device-local memory for the ESP32 runtime.
//
// This is intentionally not a full LLM/vector memory. It stores a bounded set
// of user-approved facts/preferences in NVS so the device can preserve useful
// state across reboots. Gateway/RDK memory synchronization belongs to a later
// transport layer.
class MossMemoryStore {
public:
    static constexpr size_t kMaxEntries = 24;
    static constexpr size_t kMaxKeyBytes = 40;
    static constexpr size_t kMaxValueBytes = 256;
    static constexpr size_t kMaxDocumentBytes = 3072;

    static MossMemoryStore& GetInstance() {
        static MossMemoryStore instance;
        return instance;
    }

    bool Load() {
        std::lock_guard<std::mutex> lock(mutex_);
        loaded_ = false;
        return EnsureLoadedLocked();
    }

    bool Set(const std::string& key,
             const std::string& value,
             const std::string& category,
             std::string* error = nullptr) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!ValidateKey(key, error) || !ValidateValue(value, error) ||
            !ValidateCategory(category, error)) {
            return false;
        }
        if (!EnsureLoadedLocked()) {
            SetError(error, "memory document is unhealthy; clear it explicitly before writing");
            return false;
        }

        auto candidate = entries_;
        auto it = std::find_if(candidate.begin(), candidate.end(),
            [&key](const MemoryEntry& entry) { return entry.key == key; });

        if (it != candidate.end()) {
            if (it->value == value && it->category == category) {
                return true;
            }
            it->value = value;
            it->category = category;
            it->revision = revision_ + 1;
        } else {
            if (candidate.size() >= kMaxEntries) {
                SetError(error, "memory entry limit reached");
                return false;
            }
            candidate.push_back(MemoryEntry{key, category, value, revision_ + 1});
        }

        const uint32_t next_revision = revision_ + 1;
        const std::string document = Serialize(candidate, next_revision);
        if (document.empty()) {
            SetError(error, "failed to serialize memory document");
            return false;
        }
        if (document.size() > kMaxDocumentBytes) {
            SetError(error, "memory document size limit reached");
            return false;
        }

        PersistLocked(document);
        entries_ = std::move(candidate);
        revision_ = next_revision;
        healthy_ = true;
        load_error_.clear();
        return true;
    }

    bool Remove(const std::string& key, std::string* error = nullptr) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!ValidateKey(key, error)) {
            return false;
        }
        if (!EnsureLoadedLocked()) {
            SetError(error, "memory document is unhealthy; clear it explicitly before writing");
            return false;
        }

        auto candidate = entries_;
        auto it = std::find_if(candidate.begin(), candidate.end(),
            [&key](const MemoryEntry& entry) { return entry.key == key; });
        if (it == candidate.end()) {
            SetError(error, "memory key not found");
            return false;
        }
        candidate.erase(it);

        const uint32_t next_revision = revision_ + 1;
        const std::string document = Serialize(candidate, next_revision);
        if (document.empty() || document.size() > kMaxDocumentBytes) {
            SetError(error, "failed to serialize memory document");
            return false;
        }

        PersistLocked(document);
        entries_ = std::move(candidate);
        revision_ = next_revision;
        return true;
    }

    bool Clear() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (EnsureLoadedLocked() && entries_.empty()) {
            return true;
        }

        const uint32_t next_revision = healthy_ ? revision_ + 1 : 1;
        const std::vector<MemoryEntry> empty;
        const std::string document = Serialize(empty, next_revision);
        if (document.empty()) {
            return false;
        }

        PersistLocked(document);
        entries_.clear();
        revision_ = next_revision;
        healthy_ = true;
        loaded_ = true;
        load_error_.clear();
        return true;
    }

    std::string GetJson(const std::string& key) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!EnsureLoadedLocked()) {
            return ErrorJsonLocked(load_error_);
        }
        auto it = std::find_if(entries_.begin(), entries_.end(),
            [&key](const MemoryEntry& entry) { return entry.key == key; });

        cJSON* root = cJSON_CreateObject();
        cJSON_AddBoolToObject(root, "ok", true);
        cJSON_AddStringToObject(root, "key", key.c_str());
        if (it == entries_.end()) {
            cJSON_AddBoolToObject(root, "found", false);
        } else {
            cJSON_AddBoolToObject(root, "found", true);
            AddEntryJson(root, *it);
        }
        const std::string result = PrintJson(root);
        cJSON_Delete(root);
        return result;
    }

    std::string ListJson() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!EnsureLoadedLocked()) {
            return ErrorJsonLocked(load_error_);
        }

        cJSON* root = cJSON_CreateObject();
        cJSON_AddBoolToObject(root, "ok", true);
        cJSON_AddStringToObject(root, "scope", "device-local");
        cJSON_AddBoolToObject(root, "automatic_learning", false);
        cJSON_AddNumberToObject(root, "revision", revision_);
        cJSON_AddNumberToObject(root, "count", entries_.size());
        cJSON* items = cJSON_AddArrayToObject(root, "entries");
        for (const auto& entry : entries_) {
            cJSON* item = cJSON_CreateObject();
            AddEntryJson(item, entry);
            cJSON_AddItemToArray(items, item);
        }
        const std::string result = PrintJson(root);
        cJSON_Delete(root);
        return result;
    }

    std::string StatusJson() {
        std::lock_guard<std::mutex> lock(mutex_);
        EnsureLoadedLocked();

        cJSON* root = cJSON_CreateObject();
        cJSON_AddBoolToObject(root, "ok", healthy_);
        cJSON_AddStringToObject(root, "scope", "device-local");
        cJSON_AddStringToObject(root, "storage", "nvs");
        cJSON_AddBoolToObject(root, "automatic_learning", false);
        cJSON_AddNumberToObject(root, "revision", revision_);
        cJSON_AddNumberToObject(root, "entries", entries_.size());
        cJSON_AddNumberToObject(root, "max_entries", kMaxEntries);
        cJSON_AddNumberToObject(root, "document_bytes", raw_size_);
        cJSON_AddNumberToObject(root, "max_document_bytes", kMaxDocumentBytes);
        cJSON_AddNumberToObject(root, "max_key_bytes", kMaxKeyBytes);
        cJSON_AddNumberToObject(root, "max_value_bytes", kMaxValueBytes);
        if (!load_error_.empty()) {
            cJSON_AddStringToObject(root, "error", load_error_.c_str());
        }
        const std::string result = PrintJson(root);
        cJSON_Delete(root);
        return result;
    }

    static std::string ErrorJson(const std::string& message) {
        cJSON* root = cJSON_CreateObject();
        cJSON_AddBoolToObject(root, "ok", false);
        cJSON_AddStringToObject(root, "error", message.c_str());
        const std::string result = PrintJson(root);
        cJSON_Delete(root);
        return result;
    }

private:
    MossMemoryStore() = default;
    MossMemoryStore(const MossMemoryStore&) = delete;
    MossMemoryStore& operator=(const MossMemoryStore&) = delete;

    static constexpr const char* kNamespace = "moss_memory";
    static constexpr const char* kDocumentKey = "document";
    static constexpr int kSchemaVersion = 1;

    bool EnsureLoadedLocked() {
        if (loaded_) {
            return healthy_;
        }

        Settings settings(kNamespace);
        const std::string document = settings.GetString(kDocumentKey, "");
        raw_size_ = document.size();
        entries_.clear();
        revision_ = 0;
        healthy_ = true;
        load_error_.clear();
        loaded_ = true;

        if (document.empty()) {
            return true;
        }
        if (document.size() > kMaxDocumentBytes) {
            return MarkCorruptLocked("stored memory document exceeds size limit");
        }

        cJSON* root = cJSON_Parse(document.c_str());
        if (!root || !cJSON_IsObject(root)) {
            if (root) {
                cJSON_Delete(root);
            }
            return MarkCorruptLocked("stored memory document is invalid JSON");
        }

        const cJSON* version = cJSON_GetObjectItem(root, "version");
        const cJSON* revision = cJSON_GetObjectItem(root, "revision");
        const cJSON* items = cJSON_GetObjectItem(root, "entries");
        if (!cJSON_IsNumber(version) || version->valueint != kSchemaVersion ||
            !cJSON_IsNumber(revision) || !cJSON_IsArray(items)) {
            cJSON_Delete(root);
            return MarkCorruptLocked("stored memory document has an unsupported schema");
        }

        const int count = cJSON_GetArraySize(items);
        if (count < 0 || static_cast<size_t>(count) > kMaxEntries) {
            cJSON_Delete(root);
            return MarkCorruptLocked("stored memory document exceeds entry limit");
        }

        std::vector<MemoryEntry> parsed;
        parsed.reserve(static_cast<size_t>(count));
        for (int i = 0; i < count; ++i) {
            const cJSON* item = cJSON_GetArrayItem(items, i);
            const cJSON* key = cJSON_GetObjectItem(item, "key");
            const cJSON* category = cJSON_GetObjectItem(item, "category");
            const cJSON* value = cJSON_GetObjectItem(item, "value");
            const cJSON* entry_revision = cJSON_GetObjectItem(item, "revision");
            if (!cJSON_IsObject(item) || !cJSON_IsString(key) ||
                !cJSON_IsString(category) || !cJSON_IsString(value) ||
                !cJSON_IsNumber(entry_revision)) {
                cJSON_Delete(root);
                return MarkCorruptLocked("stored memory entry is malformed");
            }

            std::string validation_error;
            const std::string key_string = key->valuestring;
            const std::string category_string = category->valuestring;
            const std::string value_string = value->valuestring;
            if (!ValidateKey(key_string, &validation_error) ||
                !ValidateCategory(category_string, &validation_error) ||
                !ValidateValue(value_string, &validation_error)) {
                cJSON_Delete(root);
                return MarkCorruptLocked("stored memory entry violates limits: " + validation_error);
            }
            const auto duplicate = std::find_if(parsed.begin(), parsed.end(),
                [&key_string](const MemoryEntry& entry) { return entry.key == key_string; });
            if (duplicate != parsed.end()) {
                cJSON_Delete(root);
                return MarkCorruptLocked("stored memory document contains duplicate keys");
            }
            parsed.push_back(MemoryEntry{
                key_string,
                category_string,
                value_string,
                static_cast<uint32_t>(entry_revision->valuedouble),
            });
        }

        revision_ = static_cast<uint32_t>(revision->valuedouble);
        entries_ = std::move(parsed);
        cJSON_Delete(root);
        return true;
    }

    bool MarkCorruptLocked(const std::string& message) {
        healthy_ = false;
        entries_.clear();
        revision_ = 0;
        load_error_ = message;
        ESP_LOGE("MossMemory", "%s", message.c_str());
        return false;
    }

    void PersistLocked(const std::string& document) {
        Settings settings(kNamespace, true);
        settings.SetString(kDocumentKey, document);
        raw_size_ = document.size();
    }

    static std::string Serialize(const std::vector<MemoryEntry>& entries, uint32_t revision) {
        cJSON* root = cJSON_CreateObject();
        if (!root) {
            return {};
        }
        cJSON_AddNumberToObject(root, "version", kSchemaVersion);
        cJSON_AddNumberToObject(root, "revision", revision);
        cJSON* items = cJSON_AddArrayToObject(root, "entries");
        if (!items) {
            cJSON_Delete(root);
            return {};
        }
        for (const auto& entry : entries) {
            cJSON* item = cJSON_CreateObject();
            cJSON_AddStringToObject(item, "key", entry.key.c_str());
            cJSON_AddStringToObject(item, "category", entry.category.c_str());
            cJSON_AddStringToObject(item, "value", entry.value.c_str());
            cJSON_AddNumberToObject(item, "revision", entry.revision);
            cJSON_AddItemToArray(items, item);
        }
        const std::string result = PrintJson(root);
        cJSON_Delete(root);
        return result;
    }

    static void AddEntryJson(cJSON* object, const MemoryEntry& entry) {
        cJSON_AddStringToObject(object, "key", entry.key.c_str());
        cJSON_AddStringToObject(object, "category", entry.category.c_str());
        cJSON_AddStringToObject(object, "value", entry.value.c_str());
        cJSON_AddNumberToObject(object, "revision", entry.revision);
    }

    static bool ValidateKey(const std::string& key, std::string* error) {
        if (key.empty() || key.size() > kMaxKeyBytes) {
            SetError(error, "key must be 1-40 bytes");
            return false;
        }
        for (unsigned char ch : key) {
            if (!(std::isalnum(ch) || ch == '_' || ch == '-' || ch == '.')) {
                SetError(error, "key may only contain A-Z, a-z, 0-9, _, - and .");
                return false;
            }
        }
        return true;
    }

    static bool ValidateValue(const std::string& value, std::string* error) {
        if (value.empty() || value.size() > kMaxValueBytes) {
            SetError(error, "value must be 1-256 bytes");
            return false;
        }
        return true;
    }

    static bool ValidateCategory(const std::string& category, std::string* error) {
        static const char* const allowed[] = {
            "profile", "preference", "fact", "routine", "device"
        };
        for (const char* item : allowed) {
            if (category == item) {
                return true;
            }
        }
        SetError(error, "category must be profile, preference, fact, routine or device");
        return false;
    }

    static void SetError(std::string* error, const std::string& message) {
        if (error) {
            *error = message;
        }
    }

    std::string ErrorJsonLocked(const std::string& message) const {
        return ErrorJson(message.empty() ? "memory unavailable" : message);
    }

    static std::string PrintJson(cJSON* root) {
        char* raw = cJSON_PrintUnformatted(root);
        std::string result = raw ? raw : "{}";
        if (raw) {
            cJSON_free(raw);
        }
        return result;
    }

    mutable std::mutex mutex_;
    bool loaded_ = false;
    bool healthy_ = true;
    uint32_t revision_ = 0;
    size_t raw_size_ = 0;
    std::string load_error_;
    std::vector<MemoryEntry> entries_;
};

}  // namespace moss::memory

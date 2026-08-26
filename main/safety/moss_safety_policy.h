#pragma once

#include <cstdint>
#include <cstdio>
#include <mutex>
#include <string>

#include <cJSON.h>
#include <esp_random.h>
#include <esp_timer.h>

namespace moss::safety {

enum class RiskLevel : uint8_t {
    ReadOnly = 0,
    LowImpact = 1,
    Sensitive = 2,
    Physical = 3,
    Destructive = 4,
    Unknown = 5,
};

struct GateDecision {
    bool allowed = false;
    RiskLevel risk = RiskLevel::Unknown;
    bool grant_consumed = false;
    std::string message;
};

struct AuthorizationChallenge {
    bool ok = false;
    std::string tool_name;
    RiskLevel risk = RiskLevel::Unknown;
    std::string local_code;
    int ttl_seconds = 0;
    std::string error;
};

// Central device-side safety gate for MCP tool execution.
//
// Standard policy intentionally allows known read-only, low-impact and
// sensitive software operations while requiring explicit local approval for
// physical, destructive and previously-unknown tools. Every known tool is
// named explicitly; future names fail closed as Unknown until reviewed.
//
// Approval is local-display based. The code is never exposed through MCP.
// Grants live only in RAM, target one exact tool, expire quickly and are
// consumed by the next matching call.
class MossSafetyPolicy {
public:
    static constexpr int kChallengeTtlSeconds = 60;
    static constexpr int kGrantTtlSeconds = 30;
    static constexpr int kMaxAuthorizationAttempts = 5;

    static MossSafetyPolicy& GetInstance() {
        static MossSafetyPolicy instance;
        return instance;
    }

    GateDecision CheckAndConsume(const std::string& tool_name) {
        std::lock_guard<std::mutex> lock(mutex_);
        const int64_t now = NowUs();
        ExpireLocked(now);

        const RiskLevel risk = Classify(tool_name);
        if (!RequiresAuthorization(risk)) {
            RecordDecisionLocked(tool_name, risk, "allowed");
            return GateDecision{true, risk, false, "allowed"};
        }

        if (!grant_tool_.empty() && grant_tool_ == tool_name && now <= grant_expires_at_us_) {
            grant_tool_.clear();
            grant_expires_at_us_ = 0;
            ++grants_consumed_;
            RecordDecisionLocked(tool_name, risk, "approved_once");
            return GateDecision{true, risk, true, "local approval consumed"};
        }

        ++blocked_calls_;
        RecordDecisionLocked(tool_name, risk, "blocked");
        return GateDecision{
            false,
            risk,
            false,
            "MOSS safety approval required for this tool; request local-display authorization first"
        };
    }

    AuthorizationChallenge RequestAuthorization(const std::string& tool_name,
                                                bool local_display_available) {
        std::lock_guard<std::mutex> lock(mutex_);
        const int64_t now = NowUs();
        ExpireLocked(now);

        const RiskLevel risk = Classify(tool_name);
        if (!RequiresAuthorization(risk)) {
            return AuthorizationChallenge{
                false, tool_name, risk, "", 0,
                "this tool does not require local safety approval"
            };
        }
        if (!local_display_available) {
            ++blocked_calls_;
            RecordDecisionLocked(tool_name, risk, "approval_unavailable");
            return AuthorizationChallenge{
                false, tool_name, risk, "", 0,
                "local approval unavailable: this device has no usable display"
            };
        }

        char code[7];
        const uint32_t value = esp_random() % 1000000U;
        std::snprintf(code, sizeof(code), "%06lu", static_cast<unsigned long>(value));

        pending_tool_ = tool_name;
        pending_code_ = code;
        pending_risk_ = risk;
        pending_expires_at_us_ = now + SecondsToUs(kChallengeTtlSeconds);
        pending_attempts_ = 0;

        // Revoke any older grant whenever a new challenge is requested.
        grant_tool_.clear();
        grant_expires_at_us_ = 0;

        ++challenges_issued_;
        RecordDecisionLocked(tool_name, risk, "challenge_issued");
        return AuthorizationChallenge{
            true, tool_name, risk, code, kChallengeTtlSeconds, ""
        };
    }

    bool Authorize(const std::string& tool_name,
                   const std::string& local_code,
                   const std::string& confirm,
                   std::string* error = nullptr) {
        std::lock_guard<std::mutex> lock(mutex_);
        const int64_t now = NowUs();
        ExpireLocked(now);

        if (confirm != "CONFIRM") {
            SetError(error, "explicit confirm=CONFIRM is required");
            return false;
        }
        if (pending_tool_.empty()) {
            SetError(error, "no active local authorization challenge");
            return false;
        }
        if (pending_tool_ != tool_name) {
            RegisterFailedAttemptLocked();
            SetError(error, "authorization target does not match pending challenge");
            return false;
        }
        if (pending_code_ != local_code) {
            RegisterFailedAttemptLocked();
            if (pending_tool_.empty()) {
                SetError(error, "invalid local code; challenge revoked after too many attempts");
            } else {
                SetError(error, "invalid local authorization code");
            }
            return false;
        }

        grant_tool_ = pending_tool_;
        grant_expires_at_us_ = now + SecondsToUs(kGrantTtlSeconds);
        ++grants_issued_;
        RecordDecisionLocked(grant_tool_, pending_risk_, "grant_issued");
        ClearPendingLocked();
        return true;
    }

    void Revoke() {
        std::lock_guard<std::mutex> lock(mutex_);
        ClearPendingLocked();
        grant_tool_.clear();
        grant_expires_at_us_ = 0;
    }

    std::string StatusJson() {
        std::lock_guard<std::mutex> lock(mutex_);
        const int64_t now = NowUs();
        ExpireLocked(now);

        cJSON* root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "schema", "moss-safety/1.0");
        cJSON_AddStringToObject(root, "mode", "standard");
        cJSON_AddStringToObject(root, "approval_channel", "local_display");
        cJSON_AddBoolToObject(root, "unknown_tools_guarded", true);
        cJSON_AddBoolToObject(root, "approval_code_exposed_via_mcp", false);
        cJSON_AddNumberToObject(root, "challenge_ttl_seconds", kChallengeTtlSeconds);
        cJSON_AddNumberToObject(root, "grant_ttl_seconds", kGrantTtlSeconds);
        cJSON_AddNumberToObject(root, "max_authorization_attempts", kMaxAuthorizationAttempts);

        cJSON* pending = cJSON_AddObjectToObject(root, "pending");
        cJSON_AddBoolToObject(pending, "active", !pending_tool_.empty());
        if (!pending_tool_.empty()) {
            cJSON_AddStringToObject(pending, "tool", pending_tool_.c_str());
            cJSON_AddStringToObject(pending, "risk", RiskName(pending_risk_));
            cJSON_AddNumberToObject(pending, "seconds_remaining",
                SecondsRemaining(now, pending_expires_at_us_));
            cJSON_AddNumberToObject(pending, "failed_attempts", pending_attempts_);
        }

        cJSON* grant = cJSON_AddObjectToObject(root, "grant");
        cJSON_AddBoolToObject(grant, "active", !grant_tool_.empty());
        if (!grant_tool_.empty()) {
            cJSON_AddStringToObject(grant, "tool", grant_tool_.c_str());
            cJSON_AddNumberToObject(grant, "seconds_remaining",
                SecondsRemaining(now, grant_expires_at_us_));
            cJSON_AddBoolToObject(grant, "one_shot", true);
        }

        cJSON* audit = cJSON_AddObjectToObject(root, "audit");
        cJSON_AddNumberToObject(audit, "sequence", audit_sequence_);
        cJSON_AddNumberToObject(audit, "blocked_calls", blocked_calls_);
        cJSON_AddNumberToObject(audit, "challenges_issued", challenges_issued_);
        cJSON_AddNumberToObject(audit, "grants_issued", grants_issued_);
        cJSON_AddNumberToObject(audit, "grants_consumed", grants_consumed_);
        if (!last_tool_.empty()) {
            cJSON_AddStringToObject(audit, "last_tool", last_tool_.c_str());
            cJSON_AddStringToObject(audit, "last_risk", RiskName(last_risk_));
            cJSON_AddStringToObject(audit, "last_decision", last_decision_.c_str());
        }

        const std::string result = PrintJson(root);
        cJSON_Delete(root);
        return result;
    }

    std::string ClassifyJson(const std::string& tool_name) const {
        const RiskLevel risk = Classify(tool_name);
        cJSON* root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "tool", tool_name.c_str());
        cJSON_AddStringToObject(root, "risk", RiskName(risk));
        cJSON_AddBoolToObject(root, "requires_local_approval", RequiresAuthorization(risk));
        const std::string result = PrintJson(root);
        cJSON_Delete(root);
        return result;
    }

    static RiskLevel Classify(const std::string& tool_name) {
        if (tool_name == "moss.safety.status" ||
            tool_name == "moss.safety.classify" ||
            tool_name == "self.get_device_status" ||
            tool_name == "self.lampbar.get_status" ||
            tool_name == "self.lampeye.get_status" ||
            tool_name == "moss.agent.get_status" ||
            tool_name == "moss.agent.get_contract" ||
            tool_name == "moss.memory.status" ||
            tool_name == "moss.memory.list" ||
            tool_name == "moss.memory.get" ||
            tool_name == "moss.hardware.profile" ||
            tool_name == "moss.hardware.status") {
            return RiskLevel::ReadOnly;
        }

        if (tool_name == "moss.safety.request" ||
            tool_name == "moss.safety.authorize" ||
            tool_name == "moss.safety.revoke" ||
            tool_name == "self.audio_speaker.set_volume" ||
            tool_name == "self.screen.set_brightness" ||
            tool_name == "self.screen.set_theme" ||
            tool_name == "code_display" ||
            tool_name == "open_browser_chat_win" ||
            tool_name == "self.lampbar.blink" ||
            tool_name == "self.lampbar.breathe" ||
            tool_name == "self.lampbar.scroll" ||
            tool_name == "self.lampbar.rainbow" ||
            tool_name == "self.lampbar.turn_on" ||
            tool_name == "self.lampbar.turn_off" ||
            tool_name == "self.lampeye.set_rgb" ||
            tool_name == "self.lampeye.set_brightness" ||
            tool_name == "self.lampeye.blink" ||
            tool_name == "self.lampeye.breathe" ||
            tool_name == "self.lampeye.turn_on" ||
            tool_name == "self.lampeye.turn_off") {
            return RiskLevel::LowImpact;
        }

        if (tool_name == "self.camera.take_photo" ||
            tool_name == "moss.memory.set" ||
            tool_name == "moss.memory.remove" ||
            tool_name == "moss.agent.set_backend") {
            return RiskLevel::Sensitive;
        }

        if (tool_name == "self.motor.control" ||
            tool_name == "self.infrared.control") {
            return RiskLevel::Physical;
        }

        if (tool_name == "moss.memory.clear") {
            return RiskLevel::Destructive;
        }

        return RiskLevel::Unknown;
    }

    static const char* RiskName(RiskLevel risk) {
        switch (risk) {
            case RiskLevel::ReadOnly: return "read_only";
            case RiskLevel::LowImpact: return "low_impact";
            case RiskLevel::Sensitive: return "sensitive";
            case RiskLevel::Physical: return "physical";
            case RiskLevel::Destructive: return "destructive";
            case RiskLevel::Unknown: return "unknown";
            default: return "unknown";
        }
    }

    static bool RequiresAuthorization(RiskLevel risk) {
        return risk == RiskLevel::Physical ||
               risk == RiskLevel::Destructive ||
               risk == RiskLevel::Unknown;
    }

private:
    MossSafetyPolicy() = default;
    MossSafetyPolicy(const MossSafetyPolicy&) = delete;
    MossSafetyPolicy& operator=(const MossSafetyPolicy&) = delete;

    static int64_t NowUs() {
        return esp_timer_get_time();
    }

    static int64_t SecondsToUs(int seconds) {
        return static_cast<int64_t>(seconds) * 1000000LL;
    }

    static int SecondsRemaining(int64_t now, int64_t expires_at) {
        if (expires_at <= now) {
            return 0;
        }
        const int64_t remaining = expires_at - now;
        return static_cast<int>((remaining + 999999LL) / 1000000LL);
    }

    void ExpireLocked(int64_t now) {
        if (!pending_tool_.empty() && now > pending_expires_at_us_) {
            ClearPendingLocked();
        }
        if (!grant_tool_.empty() && now > grant_expires_at_us_) {
            grant_tool_.clear();
            grant_expires_at_us_ = 0;
        }
    }

    void RegisterFailedAttemptLocked() {
        ++pending_attempts_;
        if (pending_attempts_ >= kMaxAuthorizationAttempts) {
            ClearPendingLocked();
        }
    }

    void ClearPendingLocked() {
        pending_tool_.clear();
        pending_code_.clear();
        pending_risk_ = RiskLevel::Unknown;
        pending_expires_at_us_ = 0;
        pending_attempts_ = 0;
    }

    void RecordDecisionLocked(const std::string& tool_name,
                              RiskLevel risk,
                              const char* decision) {
        ++audit_sequence_;
        last_tool_ = tool_name;
        last_risk_ = risk;
        last_decision_ = decision;
    }

    static void SetError(std::string* error, const std::string& message) {
        if (error) {
            *error = message;
        }
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

    std::string pending_tool_;
    std::string pending_code_;
    RiskLevel pending_risk_ = RiskLevel::Unknown;
    int64_t pending_expires_at_us_ = 0;
    int pending_attempts_ = 0;

    std::string grant_tool_;
    int64_t grant_expires_at_us_ = 0;

    uint32_t audit_sequence_ = 0;
    uint32_t blocked_calls_ = 0;
    uint32_t challenges_issued_ = 0;
    uint32_t grants_issued_ = 0;
    uint32_t grants_consumed_ = 0;
    std::string last_tool_;
    RiskLevel last_risk_ = RiskLevel::Unknown;
    std::string last_decision_;
};

}  // namespace moss::safety

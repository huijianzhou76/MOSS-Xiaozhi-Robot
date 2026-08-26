#pragma once

#include <string>

#include <esp_log.h>

#include "agent/moss_agent_adapter.h"
#include "board.h"
#include "settings.h"

namespace moss::vision {

class MossVisionBootstrap {
public:
    static bool ConfigureFromGateway() {
        auto& adapter = moss::agent::MossAgentAdapter::GetInstance();
        adapter.Load();
        if (adapter.backend() != moss::agent::AgentBackend::MossGateway) {
            return false;
        }

        auto* camera = Board::GetInstance().GetCamera();
        if (!camera) {
            return false;
        }

        Settings settings("moss_gateway");
        const std::string gateway_url = settings.GetString("url", "");
        const std::string device_token = settings.GetString("token", "");
        if (gateway_url.empty() || device_token.empty()) {
            return false;
        }

        const std::string explain_url = DeriveExplainUrl(gateway_url);
        if (explain_url.empty()) {
            ESP_LOGW("MossVision", "unable to derive vision endpoint from gateway URL");
            return false;
        }

        // Reuse the existing Gateway device credential. It stays in device NVS
        // and is only sent in the HTTP Authorization header by Camera::Explain.
        camera->SetExplainUrl(explain_url, device_token);
        ESP_LOGI("MossVision", "configured same-origin MOSS vision endpoint");
        return true;
    }

    static std::string DeriveExplainUrl(const std::string& gateway_url) {
        std::string clean = gateway_url;
        const size_t query_or_fragment = clean.find_first_of("?#");
        if (query_or_fragment != std::string::npos) {
            clean.resize(query_or_fragment);
        }

        const bool secure = clean.rfind("wss://", 0) == 0;
        const bool plain = clean.rfind("ws://", 0) == 0;
        if (!secure && !plain) {
            return {};
        }

        const size_t scheme_end = clean.find("://");
        const size_t authority_start = scheme_end + 3;
        const size_t path_start = clean.find('/', authority_start);
        const std::string authority = clean.substr(
            authority_start,
            path_start == std::string::npos ? std::string::npos : path_start - authority_start);
        if (authority.empty() || authority.find('@') != std::string::npos) {
            return {};
        }

        std::string path = path_start == std::string::npos ? "" : clean.substr(path_start);
        constexpr const char* kDeviceSuffix = "/ws/device";
        const size_t suffix_size = std::char_traits<char>::length(kDeviceSuffix);
        std::string prefix;
        if (path.empty() || path == "/") {
            prefix.clear();
        } else if (path.size() >= suffix_size &&
                   path.compare(path.size() - suffix_size, suffix_size, kDeviceSuffix) == 0) {
            prefix = path.substr(0, path.size() - suffix_size);
        } else {
            return {};
        }

        return std::string(secure ? "https://" : "http://") + authority +
               prefix + "/api/v1/vision/explain";
    }
};

}  // namespace moss::vision

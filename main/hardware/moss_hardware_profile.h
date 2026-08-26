#pragma once

#include <string>

#include <cJSON.h>

#include "audio_codec.h"
#include "board.h"
#include "display.h"
#include "system_info.h"

namespace moss::hardware {

class MossHardwareProfile {
public:
    static std::string BuildProfileJson(bool include_identifiers = false) {
        auto& board = Board::GetInstance();
        auto* codec = board.GetAudioCodec();
        auto* display = board.GetDisplay();
        auto* camera = board.GetCamera();

        cJSON* root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "schema", "moss-hardware/1.0");
        cJSON_AddStringToObject(root, "board_type", board.GetBoardType().c_str());
#ifdef BOARD_NAME
        cJSON_AddStringToObject(root, "board_name", BOARD_NAME);
#endif
        cJSON_AddBoolToObject(root, "identifiers_included", include_identifiers);

        cJSON* chip = cJSON_AddObjectToObject(root, "chip");
        cJSON_AddStringToObject(chip, "model", SystemInfo::GetChipModelName().c_str());
        cJSON_AddNumberToObject(chip, "flash_bytes", SystemInfo::GetFlashSize());

        if (include_identifiers) {
            cJSON* identifiers = cJSON_AddObjectToObject(root, "identifiers");
            cJSON_AddStringToObject(identifiers, "uuid", board.GetUuid().c_str());
            cJSON_AddStringToObject(identifiers, "mac", SystemInfo::GetMacAddress().c_str());
        }

        cJSON* audio = cJSON_AddObjectToObject(root, "audio");
        cJSON_AddBoolToObject(audio, "present", codec != nullptr);
        if (codec) {
            cJSON_AddBoolToObject(audio, "duplex", codec->duplex());
            cJSON_AddBoolToObject(audio, "input_reference", codec->input_reference());
            cJSON_AddNumberToObject(audio, "input_sample_rate", codec->input_sample_rate());
            cJSON_AddNumberToObject(audio, "output_sample_rate", codec->output_sample_rate());
            cJSON_AddNumberToObject(audio, "input_channels", codec->input_channels());
            cJSON_AddNumberToObject(audio, "output_channels", codec->output_channels());
        }

        const bool has_display = display && display->width() > 0 && display->height() > 0;
        cJSON* screen = cJSON_AddObjectToObject(root, "display");
        cJSON_AddBoolToObject(screen, "present", has_display);
        if (has_display) {
            cJSON_AddNumberToObject(screen, "width", display->width());
            cJSON_AddNumberToObject(screen, "height", display->height());
        }

        cJSON* vision = cJSON_AddObjectToObject(root, "vision");
        cJSON_AddBoolToObject(vision, "camera", camera != nullptr);

        int battery_level = 0;
        bool charging = false;
        bool discharging = false;
        const bool has_battery = board.GetBatteryLevel(battery_level, charging, discharging);
        float temperature = 0.0f;
        const bool has_temperature = board.GetTemperature(temperature);

        cJSON* sensors = cJSON_AddObjectToObject(root, "sensors");
        cJSON_AddBoolToObject(sensors, "battery", has_battery);
        cJSON_AddBoolToObject(sensors, "temperature", has_temperature);

        cJSON* capabilities = cJSON_AddObjectToObject(root, "capabilities");
        cJSON_AddBoolToObject(capabilities, "audio_input", codec != nullptr && codec->input_channels() > 0);
        cJSON_AddBoolToObject(capabilities, "audio_output", codec != nullptr && codec->output_channels() > 0);
        cJSON_AddBoolToObject(capabilities, "display", has_display);
        cJSON_AddBoolToObject(capabilities, "camera", camera != nullptr);
        cJSON_AddBoolToObject(capabilities, "battery_status", has_battery);
        cJSON_AddBoolToObject(capabilities, "temperature", has_temperature);
        cJSON_AddBoolToObject(capabilities, "mcp", true);
        cJSON_AddBoolToObject(capabilities, "device_memory", true);
        cJSON_AddBoolToObject(capabilities, "tts_stream", true);

        cJSON* privacy = cJSON_AddObjectToObject(root, "privacy");
        cJSON_AddBoolToObject(privacy, "unique_identifiers_default_redacted", true);
        cJSON_AddBoolToObject(privacy, "network_credentials_exposed", false);

        const std::string result = PrintJson(root);
        cJSON_Delete(root);
        return result;
    }

    static std::string BuildStatusJson() {
        auto& board = Board::GetInstance();
        auto* codec = board.GetAudioCodec();
        auto* display = board.GetDisplay();
        auto* backlight = board.GetBacklight();

        cJSON* root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "schema", "moss-hardware-status/1.0");
        cJSON_AddStringToObject(root, "board_type", board.GetBoardType().c_str());
        cJSON_AddNumberToObject(root, "free_heap_bytes", SystemInfo::GetFreeHeapSize());
        cJSON_AddNumberToObject(root, "minimum_free_heap_bytes", SystemInfo::GetMinimumFreeHeapSize());

        cJSON* audio = cJSON_AddObjectToObject(root, "audio");
        cJSON_AddBoolToObject(audio, "available", codec != nullptr);
        if (codec) {
            cJSON_AddBoolToObject(audio, "input_enabled", codec->input_enabled());
            cJSON_AddBoolToObject(audio, "output_enabled", codec->output_enabled());
            cJSON_AddNumberToObject(audio, "output_volume", codec->output_volume());
        }

        cJSON* screen = cJSON_AddObjectToObject(root, "display");
        const bool has_display = display && display->width() > 0 && display->height() > 0;
        cJSON_AddBoolToObject(screen, "available", has_display);
        if (has_display) {
            cJSON_AddStringToObject(screen, "theme", display->GetTheme().c_str());
        }
        if (backlight) {
            cJSON_AddNumberToObject(screen, "brightness", backlight->brightness());
        }

        int battery_level = 0;
        bool charging = false;
        bool discharging = false;
        cJSON* battery = cJSON_AddObjectToObject(root, "battery");
        const bool has_battery = board.GetBatteryLevel(battery_level, charging, discharging);
        cJSON_AddBoolToObject(battery, "available", has_battery);
        if (has_battery) {
            cJSON_AddNumberToObject(battery, "level", battery_level);
            cJSON_AddBoolToObject(battery, "charging", charging);
            cJSON_AddBoolToObject(battery, "discharging", discharging);
        }

        float temperature = 0.0f;
        cJSON* thermal = cJSON_AddObjectToObject(root, "thermal");
        const bool has_temperature = board.GetTemperature(temperature);
        cJSON_AddBoolToObject(thermal, "available", has_temperature);
        if (has_temperature) {
            cJSON_AddNumberToObject(thermal, "celsius", temperature);
        }

        AddSafeNetworkState(root, board.GetDeviceStatusJson());

        const std::string result = PrintJson(root);
        cJSON_Delete(root);
        return result;
    }

private:
    static void AddSafeNetworkState(cJSON* root, const std::string& board_status_json) {
        cJSON* network_out = cJSON_AddObjectToObject(root, "network");
        cJSON_AddBoolToObject(network_out, "available", false);

        cJSON* status = cJSON_Parse(board_status_json.c_str());
        if (!status || !cJSON_IsObject(status)) {
            if (status) {
                cJSON_Delete(status);
            }
            return;
        }

        const cJSON* network = cJSON_GetObjectItem(status, "network");
        if (cJSON_IsObject(network)) {
            cJSON_ReplaceItemInObject(network_out, "available", cJSON_CreateBool(true));
            const cJSON* type = cJSON_GetObjectItem(network, "type");
            const cJSON* signal = cJSON_GetObjectItem(network, "signal");
            if (cJSON_IsString(type)) {
                cJSON_AddStringToObject(network_out, "type", type->valuestring);
            }
            if (cJSON_IsString(signal)) {
                cJSON_AddStringToObject(network_out, "signal", signal->valuestring);
            }
        }
        cJSON_Delete(status);
    }

    static std::string PrintJson(cJSON* root) {
        char* raw = cJSON_PrintUnformatted(root);
        std::string result = raw ? raw : "{}";
        if (raw) {
            cJSON_free(raw);
        }
        return result;
    }
};

}  // namespace moss::hardware

#include "mcp_tools.h"
#include "hardware/moss_hardware_profile.h"

#include <esp_log.h>

namespace mcp_tools {

class MossHardwareProfileTools : public McpTool {
public:
    MossHardwareProfileTools()
        : McpTool("moss.hardware", "MOSS hardware capability and live status") {}

    static MossHardwareProfileTools& GetInstance() {
        static MossHardwareProfileTools instance;
        return instance;
    }

    void Register() override {
        ESP_LOGI("MossHardware", "register MOSS hardware profile tools");
        auto& server = McpServer::GetInstance();

        server.AddTool(
            "moss.hardware.profile",
            "读取当前设备的 MOSS 硬件能力画像，包括板型、芯片资源、音频、显示、摄像头、电池与温度能力。默认隐藏 UUID/MAC；只有明确需要设备诊断时才将 include_identifiers 设为 true。不会返回 Wi-Fi SSID、IP 或网络凭据。",
            PropertyList({
                Property("include_identifiers", kPropertyTypeBoolean, false),
            }),
            [](const PropertyList& properties) -> ReturnValue {
                const bool include_identifiers = properties["include_identifiers"].value<bool>();
                return moss::hardware::MossHardwareProfile::BuildProfileJson(include_identifiers);
            });

        server.AddTool(
            "moss.hardware.status",
            "读取当前设备的实时硬件状态，包括可用堆内存、音频输入输出状态、音量、屏幕、电池、温度与脱敏网络信号。不会返回 SSID、IP、MAC 或 UUID。",
            PropertyList(),
            [](const PropertyList&) -> ReturnValue {
                return moss::hardware::MossHardwareProfile::BuildStatusJson();
            });
    }
};

}  // namespace mcp_tools

static auto& g_moss_hardware_profile_tools = mcp_tools::MossHardwareProfileTools::GetInstance();
DECLARE_MCP_TOOL_INSTANCE(g_moss_hardware_profile_tools);

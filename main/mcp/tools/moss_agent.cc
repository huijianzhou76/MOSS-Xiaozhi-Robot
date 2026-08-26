#include "mcp_tools.h"
#include "agent/moss_agent_adapter.h"
#include "application.h"

#include <esp_log.h>
#include <string>

namespace mcp_tools {

namespace {

moss::agent::AgentPhase MapDevicePhase(DeviceState state) {
    switch (state) {
        case kDeviceStateListening:
            return moss::agent::AgentPhase::Listening;
        case kDeviceStateSpeaking:
            return moss::agent::AgentPhase::Speaking;
        case kDeviceStateFatalError:
            return moss::agent::AgentPhase::Error;
        case kDeviceStateUnknown:
        case kDeviceStateStarting:
        case kDeviceStateWifiConfiguring:
        case kDeviceStateIdle:
        case kDeviceStateConnecting:
        case kDeviceStateUpgrading:
        case kDeviceStateActivating:
        default:
            return moss::agent::AgentPhase::Idle;
    }
}

void SyncWithApplication(moss::agent::MossAgentAdapter& adapter) {
    auto& app = Application::GetInstance();
    adapter.SyncRuntime(
        app.IsAudioChannelOpened(),
        app.GetSessionId(),
        MapDevicePhase(app.GetDeviceState()));
}

}  // namespace

class MossAgentControl : public McpTool {
public:
    MossAgentControl() : McpTool("moss.agent", "MOSS Agent backend control") {}

    static MossAgentControl& GetInstance() {
        static MossAgentControl instance;
        return instance;
    }

    void Register() override {
        ESP_LOGI("MossAgentControl", "register MOSS agent control tools");

        McpServer::GetInstance().AddTool(
            "moss.agent.get_status",
            "读取绑定到真实 Application/Protocol 的 MOSS Agent 运行状态。返回 backend、phase、audio channel、session 与事件序号；thinking/speaking/tool execution 来自实时协议和工具生命周期，而不是 UI 模拟。",
            PropertyList(),
            [](const PropertyList&) -> ReturnValue {
                auto& adapter = moss::agent::MossAgentAdapter::GetInstance();
                adapter.Load();
                SyncWithApplication(adapter);
                return adapter.StatusJson();
            });

        McpServer::GetInstance().AddTool(
            "moss.agent.set_backend",
            "切换设备端 AI 后端标识。xiaozhi 保持现有小智兼容链路；moss-gateway 为未来 RDK X5 / 自建 MOSS Core 网关模式。该设置写入 NVS。注意：本工具只切换 Agent 适配配置，不会在本阶段修改服务器 URL。",
            PropertyList({
                Property("backend", kPropertyTypeString),
            }),
            [](const PropertyList& properties) -> ReturnValue {
                auto value = properties["backend"].value<std::string>();
                auto& adapter = moss::agent::MossAgentAdapter::GetInstance();
                if (!adapter.SetBackend(value, true)) {
                    return std::string("unsupported backend; use xiaozhi or moss-gateway");
                }
                return std::string("backend saved: ") +
                    moss::agent::MossAgentAdapter::BackendName(adapter.backend());
            });

        McpServer::GetInstance().AddTool(
            "moss.agent.get_contract",
            "返回 MOSS Gateway 设备握手协议示例，用于 RDK X5 / 自建 Agent Gateway 对接。握手中的 session_id 与 runtime_state capability 来自当前设备运行时。",
            PropertyList(),
            [](const PropertyList&) -> ReturnValue {
                auto& adapter = moss::agent::MossAgentAdapter::GetInstance();
                adapter.Load();
                SyncWithApplication(adapter);
#ifdef BOARD_TYPE
                const std::string board_type = BOARD_TYPE;
#else
                const std::string board_type = "unknown";
#endif
#ifdef BOARD_NAME
                const std::string board_name = BOARD_NAME;
#else
                const std::string board_name = board_type;
#endif
                return adapter.BuildHello(board_type, board_name, true, true, true);
            });
    }
};

}  // namespace mcp_tools

static auto& g_moss_agent_control = mcp_tools::MossAgentControl::GetInstance();
DECLARE_MCP_TOOL_INSTANCE(g_moss_agent_control);

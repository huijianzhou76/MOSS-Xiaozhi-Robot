#include "mcp_tools.h"
#include "gateway/moss_gateway_client.h"

#include <esp_log.h>
#include <string>

namespace mcp_tools {

class MossGatewayTools : public McpTool {
public:
    MossGatewayTools() : McpTool("moss.gateway", "MOSS Host/RDK Gateway client control") {}

    static MossGatewayTools& GetInstance() {
        static MossGatewayTools instance;
        return instance;
    }

    void Register() override {
        ESP_LOGI("MossGatewayTools", "register MOSS Gateway client tools");
        auto& server = McpServer::GetInstance();

        server.AddTool(
            "moss.gateway.status",
            "读取 ESP32 到 MOSS Host/RDK Gateway 的客户端状态。返回配置、网络就绪、连接、welcome、自动重连、heartbeat 与消息计数，但永不返回设备 token。",
            PropertyList(),
            [](const PropertyList&) -> ReturnValue {
                return moss::gateway::MossGatewayClient::GetInstance().StatusJson();
            });

        server.AddTool(
            "moss.gateway.configure",
            "配置 MOSS Gateway WebSocket URL 与 device token，并写入本机 NVS。URL 必须为 ws:// 或 wss://，禁止 URL 内嵌 user:password；token 不会在返回值中显示。此操作不会自动切换 Agent backend。",
            PropertyList({
                Property("url", kPropertyTypeString),
                Property("token", kPropertyTypeString),
            }),
            [](const PropertyList& properties) -> ReturnValue {
                const auto url = properties["url"].value<std::string>();
                const auto token = properties["token"].value<std::string>();
                std::string error;
                auto& client = moss::gateway::MossGatewayClient::GetInstance();
                if (!client.Configure(url, token, &error)) {
                    return std::string("gateway configure failed: ") + error;
                }
                return client.StatusJson();
            });

        server.AddTool(
            "moss.gateway.start",
            "立即连接 MOSS Host/RDK Gateway，并恢复本次运行中的自动重连。要求当前 Agent backend 已显式设置为 moss-gateway；Wi-Fi 网络成功连接后自治 worker 会持续维护该连接。",
            PropertyList(),
            [](const PropertyList&) -> ReturnValue {
                std::string error;
                auto& client = moss::gateway::MossGatewayClient::GetInstance();
                if (!client.Start(&error)) {
                    return std::string("gateway start failed: ") + error;
                }
                return client.StatusJson();
            });

        server.AddTool(
            "moss.gateway.stop",
            "主动断开 ESP32 到 MOSS Gateway 的独立 WebSocket，并暂停本次运行中的自动重连；再次调用 moss.gateway.start 可恢复。不会影响现有 Xiaozhi 音频协议连接。",
            PropertyList(),
            [](const PropertyList&) -> ReturnValue {
                auto& client = moss::gateway::MossGatewayClient::GetInstance();
                client.Stop();
                return client.StatusJson();
            });

        server.AddTool(
            "moss.gateway.heartbeat",
            "向已连接的 MOSS Gateway 立即发送一次 heartbeat 与当前 Agent phase。正常情况下自治 worker 每 15 秒自动发送。",
            PropertyList(),
            [](const PropertyList&) -> ReturnValue {
                std::string error;
                auto& client = moss::gateway::MossGatewayClient::GetInstance();
                if (!client.SendHeartbeat(&error)) {
                    return std::string("gateway heartbeat failed: ") + error;
                }
                return client.StatusJson();
            });

        server.AddTool(
            "moss.gateway.sync_state",
            "向已连接的 MOSS Gateway 主动同步一次当前 moss-agent state 事件。每个新 Gateway session welcome 后自治 worker 也会自动同步一次。",
            PropertyList(),
            [](const PropertyList&) -> ReturnValue {
                std::string error;
                auto& client = moss::gateway::MossGatewayClient::GetInstance();
                if (!client.SendState(&error)) {
                    return std::string("gateway state sync failed: ") + error;
                }
                return client.StatusJson();
            });
    }
};

}  // namespace mcp_tools

static auto& g_moss_gateway_tools = mcp_tools::MossGatewayTools::GetInstance();
DECLARE_MCP_TOOL_INSTANCE(g_moss_gateway_tools);

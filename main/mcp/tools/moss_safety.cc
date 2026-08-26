#include "mcp_tools.h"
#include "safety/moss_safety_policy.h"

#include "board.h"
#include "display.h"

#include <cJSON.h>
#include <esp_log.h>
#include <string>

namespace mcp_tools {

class MossSafetyTools : public McpTool {
public:
    MossSafetyTools() : McpTool("moss.safety", "MOSS device-side execution safety gate") {}

    static MossSafetyTools& GetInstance() {
        static MossSafetyTools instance;
        return instance;
    }

    void Register() override {
        ESP_LOGI("MossSafety", "register MOSS safety tools");
        auto& server = McpServer::GetInstance();

        server.AddTool(
            "moss.safety.status",
            "读取设备端 Safety Gate 状态、待确认目标、一次性授权和审计计数。授权码绝不会通过 MCP 返回。",
            PropertyList(),
            [](const PropertyList&) -> ReturnValue {
                return moss::safety::MossSafetyPolicy::GetInstance().StatusJson();
            });

        server.AddTool(
            "moss.safety.classify",
            "查询某个 MCP 工具的设备端风险等级，以及 standard 策略下是否需要本机确认。",
            PropertyList({
                Property("tool", kPropertyTypeString),
            }),
            [](const PropertyList& properties) -> ReturnValue {
                const auto tool = properties["tool"].value<std::string>();
                return moss::safety::MossSafetyPolicy::GetInstance().ClassifyJson(tool);
            });

        server.AddTool(
            "moss.safety.request",
            "为一个受保护 MCP 工具申请一次本机授权。验证码只显示在机器人本机屏幕，不会出现在 MCP 响应、日志或 Agent 上下文中。无可用屏幕时请求会拒绝。",
            PropertyList({
                Property("tool", kPropertyTypeString),
            }),
            [](const PropertyList& properties) -> ReturnValue {
                const auto tool = properties["tool"].value<std::string>();
                auto* display = Board::GetInstance().GetDisplay();
                const bool has_local_display =
                    display != nullptr && display->width() > 0 && display->height() > 0;

                auto challenge = moss::safety::MossSafetyPolicy::GetInstance()
                    .RequestAuthorization(tool, has_local_display);
                if (!challenge.ok) {
                    return ErrorJson(challenge.error);
                }

                const std::string notification = "MOSS AUTH " + challenge.local_code;
                display->ShowNotification(notification, challenge.ttl_seconds * 1000);

                cJSON* root = cJSON_CreateObject();
                cJSON_AddBoolToObject(root, "ok", true);
                cJSON_AddStringToObject(root, "tool", challenge.tool_name.c_str());
                cJSON_AddStringToObject(root, "risk",
                    moss::safety::MossSafetyPolicy::RiskName(challenge.risk));
                cJSON_AddStringToObject(root, "confirmation_channel", "local_display");
                cJSON_AddBoolToObject(root, "code_returned_via_mcp", false);
                cJSON_AddNumberToObject(root, "expires_in_seconds", challenge.ttl_seconds);
                cJSON_AddStringToObject(root, "instruction",
                    "Read the six-digit code from the physical device display and explicitly provide it for authorization.");
                const std::string result = PrintJson(root);
                cJSON_Delete(root);
                return result;
            });

        server.AddTool(
            "moss.safety.authorize",
            "使用用户从机器人本机屏幕看到的六位验证码，为完全相同的目标工具创建一次性短时授权。还必须显式传入 confirm=CONFIRM。验证码错误最多允许 5 次。",
            PropertyList({
                Property("tool", kPropertyTypeString),
                Property("code", kPropertyTypeString),
                Property("confirm", kPropertyTypeString),
            }),
            [](const PropertyList& properties) -> ReturnValue {
                const auto tool = properties["tool"].value<std::string>();
                const auto code = properties["code"].value<std::string>();
                const auto confirm = properties["confirm"].value<std::string>();
                std::string error;
                auto& safety = moss::safety::MossSafetyPolicy::GetInstance();
                if (!safety.Authorize(tool, code, confirm, &error)) {
                    return ErrorJson(error);
                }

                cJSON* root = cJSON_CreateObject();
                cJSON_AddBoolToObject(root, "ok", true);
                cJSON_AddStringToObject(root, "tool", tool.c_str());
                cJSON_AddBoolToObject(root, "one_shot", true);
                cJSON_AddNumberToObject(root, "expires_in_seconds",
                    moss::safety::MossSafetyPolicy::kGrantTtlSeconds);
                const std::string result = PrintJson(root);
                cJSON_Delete(root);
                return result;
            });

        server.AddTool(
            "moss.safety.revoke",
            "立即撤销当前待确认验证码和未消费的一次性授权。",
            PropertyList(),
            [](const PropertyList&) -> ReturnValue {
                auto& safety = moss::safety::MossSafetyPolicy::GetInstance();
                safety.Revoke();
                return safety.StatusJson();
            });
    }

private:
    static std::string ErrorJson(const std::string& error) {
        cJSON* root = cJSON_CreateObject();
        cJSON_AddBoolToObject(root, "ok", false);
        cJSON_AddStringToObject(root, "error", error.c_str());
        const std::string result = PrintJson(root);
        cJSON_Delete(root);
        return result;
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

}  // namespace mcp_tools

static auto& g_moss_safety_tools = mcp_tools::MossSafetyTools::GetInstance();
DECLARE_MCP_TOOL_INSTANCE(g_moss_safety_tools);

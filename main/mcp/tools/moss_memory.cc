#include "mcp_tools.h"
#include "memory/moss_memory.h"

#include <esp_log.h>
#include <string>

namespace mcp_tools {

class MossMemoryTools : public McpTool {
public:
    MossMemoryTools() : McpTool("moss.memory", "MOSS device-local memory") {}

    static MossMemoryTools& GetInstance() {
        static MossMemoryTools instance;
        return instance;
    }

    void Register() override {
        ESP_LOGI("MossMemoryTools", "register MOSS memory tools");
        auto& server = McpServer::GetInstance();

        server.AddTool(
            "moss.memory.status",
            "读取 MOSS 设备端记忆层状态、容量、条目数与健康状态。该记忆保存在本机 NVS，不代表云端或 RDK X5 长期记忆。",
            PropertyList(),
            [](const PropertyList&) -> ReturnValue {
                return moss::memory::MossMemoryStore::GetInstance().StatusJson();
            });

        server.AddTool(
            "moss.memory.list",
            "列出当前设备端受控记忆。只返回已经被显式写入的本地记忆，不会自动推断或偷偷学习用户信息。",
            PropertyList(),
            [](const PropertyList&) -> ReturnValue {
                return moss::memory::MossMemoryStore::GetInstance().ListJson();
            });

        server.AddTool(
            "moss.memory.get",
            "按 key 读取一条设备端记忆。key 仅允许字母、数字、下划线、短横线与点。",
            PropertyList({
                Property("key", kPropertyTypeString),
            }),
            [](const PropertyList& properties) -> ReturnValue {
                const auto key = properties["key"].value<std::string>();
                return moss::memory::MossMemoryStore::GetInstance().GetJson(key);
            });

        server.AddTool(
            "moss.memory.set",
            "显式保存一条设备端长期记忆到 NVS。适合用户确认过的偏好、资料、事实、习惯或设备信息；不要把临时对话、秘密凭据或大段文本写入此处。",
            PropertyList({
                Property("key", kPropertyTypeString),
                Property("value", kPropertyTypeString),
                Property("category", kPropertyTypeString, std::string("fact")),
            }),
            [](const PropertyList& properties) -> ReturnValue {
                const auto key = properties["key"].value<std::string>();
                const auto value = properties["value"].value<std::string>();
                const auto category = properties["category"].value<std::string>();
                std::string error;
                auto& memory = moss::memory::MossMemoryStore::GetInstance();
                if (!memory.Set(key, value, category, &error)) {
                    return moss::memory::MossMemoryStore::ErrorJson(error);
                }
                return memory.GetJson(key);
            });

        server.AddTool(
            "moss.memory.remove",
            "删除指定 key 的一条设备端记忆。只影响 ESP32 本机 NVS 记忆。",
            PropertyList({
                Property("key", kPropertyTypeString),
            }),
            [](const PropertyList& properties) -> ReturnValue {
                const auto key = properties["key"].value<std::string>();
                std::string error;
                auto& memory = moss::memory::MossMemoryStore::GetInstance();
                if (!memory.Remove(key, &error)) {
                    return moss::memory::MossMemoryStore::ErrorJson(error);
                }
                return memory.StatusJson();
            });

        server.AddTool(
            "moss.memory.clear",
            "清空全部设备端记忆。这是破坏性操作，必须将 confirm 精确设置为 CLEAR。",
            PropertyList({
                Property("confirm", kPropertyTypeString),
            }),
            [](const PropertyList& properties) -> ReturnValue {
                const auto confirm = properties["confirm"].value<std::string>();
                if (confirm != "CLEAR") {
                    return moss::memory::MossMemoryStore::ErrorJson(
                        "refused: set confirm to CLEAR to erase device memory");
                }
                auto& memory = moss::memory::MossMemoryStore::GetInstance();
                if (!memory.Clear()) {
                    return moss::memory::MossMemoryStore::ErrorJson("failed to clear device memory");
                }
                return memory.StatusJson();
            });
    }
};

}  // namespace mcp_tools

static auto& g_moss_memory_tools = mcp_tools::MossMemoryTools::GetInstance();
DECLARE_MCP_TOOL_INSTANCE(g_moss_memory_tools);

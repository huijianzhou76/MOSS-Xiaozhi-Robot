#pragma once

#include <atomic>
#include <exception>
#include <functional>
#include <memory>
#include <string>
#include <thread>

#include <cJSON.h>
#include <esp_log.h>
#include <esp_pthread.h>

#include "mcp_server.h"

namespace moss::gateway {

class MossGatewayReadBridge {
public:
    static constexpr size_t kMaxCallIdBytes = 80;
    static constexpr size_t kMaxToolNameBytes = 120;
    static constexpr size_t kMaxArgumentsBytes = 4096;
    static constexpr int kWorkerStackSize = 8192;

    using SendFunction = std::function<bool(const std::string&)>;

    static MossGatewayReadBridge& GetInstance() {
        static MossGatewayReadBridge instance;
        return instance;
    }

    void Advertise(cJSON* hello_root) const {
        if (!hello_root || !cJSON_IsObject(hello_root)) {
            return;
        }
        cJSON* capabilities = cJSON_GetObjectItem(hello_root, "capabilities");
        if (!cJSON_IsObject(capabilities)) {
            capabilities = cJSON_AddObjectToObject(hello_root, "capabilities");
        }
        cJSON_AddBoolToObject(capabilities, "gateway_tool_bridge", true);
        cJSON_AddStringToObject(capabilities, "gateway_tool_bridge_mode", "read-only");
    }

    bool IsRemoteReadTool(const std::string& tool_name) const {
        return tool_name == "moss.agent.get_status" ||
               tool_name == "moss.agent.get_contract" ||
               tool_name == "moss.hardware.profile" ||
               tool_name == "moss.hardware.status" ||
               tool_name == "moss.memory.status" ||
               tool_name == "moss.memory.list" ||
               tool_name == "moss.memory.get" ||
               tool_name == "moss.safety.status" ||
               tool_name == "moss.safety.classify";
    }

    bool HandleToolCall(const cJSON* root,
                        const std::string& current_gateway_session_id,
                        const SendFunction& send) {
        if (!root || !cJSON_IsObject(root)) {
            return false;
        }
        const cJSON* event = cJSON_GetObjectItem(root, "event");
        if (!cJSON_IsString(event) || std::string(event->valuestring) != "tool_call") {
            return false;
        }

        const cJSON* id = cJSON_GetObjectItem(root, "id");
        const cJSON* session = cJSON_GetObjectItem(root, "gateway_session_id");
        const cJSON* name = cJSON_GetObjectItem(root, "name");
        const cJSON* arguments = cJSON_GetObjectItem(root, "arguments");

        const std::string call_id = cJSON_IsString(id) ? id->valuestring : "";
        if (call_id.empty() || call_id.size() > kMaxCallIdBytes) {
            SendError(send, call_id, current_gateway_session_id, "invalid tool call id");
            return true;
        }
        if (!cJSON_IsString(session) || current_gateway_session_id.empty() ||
            current_gateway_session_id != session->valuestring) {
            SendError(send, call_id, current_gateway_session_id, "stale or invalid gateway session");
            return true;
        }
        if (!cJSON_IsString(name)) {
            SendError(send, call_id, current_gateway_session_id, "missing remote tool name");
            return true;
        }

        const std::string tool_name = name->valuestring;
        if (tool_name.empty() || tool_name.size() > kMaxToolNameBytes ||
            !IsRemoteReadTool(tool_name)) {
            SendError(send, call_id, current_gateway_session_id,
                      "remote tool is not in device read-only allowlist");
            return true;
        }
        if (arguments != nullptr && !cJSON_IsObject(arguments)) {
            SendError(send, call_id, current_gateway_session_id, "invalid remote tool arguments");
            return true;
        }

        std::string validation_error;
        if (!ValidateArguments(tool_name, arguments, &validation_error)) {
            SendError(send, call_id, current_gateway_session_id, validation_error);
            return true;
        }

        const std::string arguments_json = arguments ? PrintJson(arguments) : "{}";
        if (arguments_json.size() > kMaxArgumentsBytes) {
            SendError(send, call_id, current_gateway_session_id,
                      "remote tool arguments exceed device size limit");
            return true;
        }

        bool expected = false;
        if (!in_flight_.compare_exchange_strong(expected, true)) {
            SendError(send, call_id, current_gateway_session_id,
                      "device read bridge already has an in-flight call");
            return true;
        }

        auto context = std::make_shared<CallContext>();
        context->call_id = call_id;
        context->gateway_session_id = current_gateway_session_id;
        context->tool_name = tool_name;
        context->arguments_json = arguments_json;
        context->send = send;

        esp_pthread_cfg_t cfg = esp_pthread_get_default_config();
        cfg.thread_name = "moss_read_bridge";
        cfg.stack_size = kWorkerStackSize;
        cfg.prio = 1;
        esp_pthread_set_cfg(&cfg);

        try {
            std::thread([this, context]() { Execute(context); }).detach();
        } catch (...) {
            in_flight_.store(false);
            SendError(send, call_id, current_gateway_session_id,
                      "failed to start device read bridge worker");
        }
        return true;
    }

private:
    struct CallContext {
        std::string call_id;
        std::string gateway_session_id;
        std::string tool_name;
        std::string arguments_json;
        SendFunction send;
    };

    MossGatewayReadBridge() = default;
    MossGatewayReadBridge(const MossGatewayReadBridge&) = delete;
    MossGatewayReadBridge& operator=(const MossGatewayReadBridge&) = delete;

    static bool HasOnlyKeys(const cJSON* object, const char* first = nullptr) {
        if (!object) {
            return true;
        }
        if (!cJSON_IsObject(object)) {
            return false;
        }
        const cJSON* child = object->child;
        while (child) {
            const std::string key = child->string ? child->string : "";
            if (!first || key != first) {
                return false;
            }
            child = child->next;
        }
        return true;
    }

    static bool ValidateArguments(const std::string& tool_name,
                                  const cJSON* arguments,
                                  std::string* error) {
        if (tool_name == "moss.memory.get") {
            if (!HasOnlyKeys(arguments, "key")) {
                SetError(error, "moss.memory.get accepts only key");
                return false;
            }
            const cJSON* key = arguments ? cJSON_GetObjectItem(arguments, "key") : nullptr;
            if (!cJSON_IsString(key) || std::string(key->valuestring).empty() ||
                std::string(key->valuestring).size() > 40) {
                SetError(error, "moss.memory.get requires a valid key");
                return false;
            }
            return true;
        }
        if (tool_name == "moss.safety.classify") {
            if (!HasOnlyKeys(arguments, "tool")) {
                SetError(error, "moss.safety.classify accepts only tool");
                return false;
            }
            const cJSON* value = arguments ? cJSON_GetObjectItem(arguments, "tool") : nullptr;
            if (!cJSON_IsString(value) || std::string(value->valuestring).empty() ||
                std::string(value->valuestring).size() > kMaxToolNameBytes) {
                SetError(error, "moss.safety.classify requires a valid tool");
                return false;
            }
            return true;
        }

        if (!HasOnlyKeys(arguments)) {
            SetError(error, "remote read tool does not accept arguments");
            return false;
        }
        return true;
    }

    void Execute(const std::shared_ptr<CallContext>& context) {
        cJSON* arguments = cJSON_Parse(context->arguments_json.c_str());
        if (!arguments || !cJSON_IsObject(arguments)) {
            if (arguments) {
                cJSON_Delete(arguments);
            }
            SendError(context->send, context->call_id,
                      context->gateway_session_id, "failed to parse remote tool arguments");
            in_flight_.store(false);
            return;
        }

        try {
            const std::string local_result =
                McpServer::GetInstance().CallToolLocal(context->tool_name, arguments);
            cJSON_Delete(arguments);
            SendSuccess(context->send, context->call_id,
                        context->gateway_session_id, NormalizeLocalResult(local_result));
        } catch (const std::exception& exc) {
            cJSON_Delete(arguments);
            std::string message = exc.what();
            if (message.size() > 500) {
                message.resize(500);
            }
            SendError(context->send, context->call_id,
                      context->gateway_session_id, message);
        } catch (...) {
            cJSON_Delete(arguments);
            SendError(context->send, context->call_id,
                      context->gateway_session_id, "device read tool failed");
        }
        in_flight_.store(false);
    }

    static cJSON* NormalizeLocalResult(const std::string& local_result) {
        cJSON* wrapper = cJSON_Parse(local_result.c_str());
        if (!wrapper || !cJSON_IsObject(wrapper)) {
            if (wrapper) {
                cJSON_Delete(wrapper);
            }
            return cJSON_CreateString(local_result.c_str());
        }

        cJSON* content = cJSON_GetObjectItem(wrapper, "content");
        cJSON* first = cJSON_IsArray(content) ? cJSON_GetArrayItem(content, 0) : nullptr;
        cJSON* text = cJSON_IsObject(first) ? cJSON_GetObjectItem(first, "text") : nullptr;
        if (!cJSON_IsString(text)) {
            return wrapper;
        }

        const std::string value = text->valuestring;
        cJSON_Delete(wrapper);
        cJSON* parsed = cJSON_Parse(value.c_str());
        if (parsed) {
            return parsed;
        }
        return cJSON_CreateString(value.c_str());
    }

    static void SendSuccess(const SendFunction& send,
                            const std::string& call_id,
                            const std::string& gateway_session_id,
                            cJSON* result) {
        cJSON* root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "event", "tool_result");
        cJSON_AddStringToObject(root, "id", call_id.c_str());
        cJSON_AddStringToObject(root, "gateway_session_id", gateway_session_id.c_str());
        cJSON_AddBoolToObject(root, "ok", true);
        cJSON_AddItemToObject(root, "result", result ? result : cJSON_CreateNull());
        const std::string payload = PrintJson(root);
        cJSON_Delete(root);
        send(payload);
    }

    static void SendError(const SendFunction& send,
                          const std::string& call_id,
                          const std::string& gateway_session_id,
                          const std::string& error) {
        cJSON* root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "event", "tool_result");
        cJSON_AddStringToObject(root, "id", call_id.c_str());
        cJSON_AddStringToObject(root, "gateway_session_id", gateway_session_id.c_str());
        cJSON_AddBoolToObject(root, "ok", false);
        cJSON_AddStringToObject(root, "error", error.c_str());
        const std::string payload = PrintJson(root);
        cJSON_Delete(root);
        send(payload);
    }

    static void SetError(std::string* error, const std::string& message) {
        if (error) {
            *error = message;
        }
    }

    static std::string PrintJson(const cJSON* root) {
        char* raw = cJSON_PrintUnformatted(root);
        std::string result = raw ? raw : "{}";
        if (raw) {
            cJSON_free(raw);
        }
        return result;
    }

    std::atomic<bool> in_flight_{false};
};

}  // namespace moss::gateway

#include "service/AgentClient.hpp"
#include "service/WorkspaceAgentTools.hpp"
#include "TestSupport.hpp"
#include <cstdlib>
#include <cstring>
#include <sys/socket.h>
#include <sys/un.h>
#include <thread>
#include <unistd.h>

using Json = nlohmann::json;

void bridgeRoundTrip() {
    char directory[] = "/tmp/irade-agent-test-XXXXXX";
    AGORA_CHECK(mkdtemp(directory));
    const std::string path = std::string(directory) + "/agent.sock";
    setenv("IRADE_AGENT_SOCKET", path.c_str(), 1);
    int server = socket(AF_UNIX, SOCK_STREAM, 0);
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    std::strcpy(address.sun_path, path.c_str());
    AGORA_CHECK(bind(server, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0);
    AGORA_CHECK(listen(server, 1) == 0);
    std::thread worker([&] {
        int fd = accept(server, nullptr, nullptr);
        FILE* io = fdopen(fd, "r+");
        char* line = nullptr;
        size_t size = 0;
        getline(&line, &size, io);
        const std::string frames =
            "{\"type\":\"output\",\"output\":\"reading\"}\n"
            "{\"type\":\"tool\",\"id\":1,\"name\":\"get_objects\",\"arguments\":{\"ids\":[\"a\"]}}\n";
        fwrite(frames.data(), 1, frames.size(), io);
        fflush(io);
        getline(&line, &size, io);
        const auto result = Json::parse(line);
        const std::string final_frame = Json{{"answer", result["result"]["text"]}}.dump() + "\n";
        fwrite(final_frame.data(), 1, final_frame.size(), io);
        fflush(io);
        free(line);
        fclose(io);
    });
    std::string progress;
    auto result = agora::agent::request(
        {{"message", "hi"}},
        [](const std::string& name, const Json& args) {
            AGORA_CHECK(name == "get_objects" && args["ids"][0] == "a");
            return Json{{"text", "done"}};
        },
        [&](const std::string& text) { progress += text; });
    worker.join();
    close(server);
    unlink(path.c_str());
    rmdir(directory);
    AGORA_CHECK(result["answer"] == "done" && progress == "reading");

    auto unavailable = agora::agent::request(Json::object(), {}, {});
    AGORA_CHECK(unavailable["error"] == "AGENT_UNAVAILABLE");

    agora::agent::options({{"reasoning_effort", "high"}, {"max_steps", 3}, {"max_tokens", 4096}});
    for (const auto& invalid : Json::array({
             Json{{"max_steps", 1.5}},
             Json{{"api_key", "forged"}},
             Json{{"reasoning_effort", "medium"}},
             Json{{"top_p", false}},
         })) {
        bool rejected = false;
        try {
            agora::agent::options(invalid);
        } catch (const std::invalid_argument&) {
            rejected = true;
        }
        AGORA_CHECK(rejected);
    }

    AGORA_CHECK(agora::agent::validApiId(""));
    AGORA_CHECK(agora::agent::validApiId("k-exaone_v2.5"));
    AGORA_CHECK(!agora::agent::validApiId("https://evil.internal"));
    AGORA_CHECK(!agora::agent::validApiId("bad api"));

    auto item = Json{
        {"kind", "code"}, {"renderMode", "irade-python"}, {"language", "python"}, {"code", "print(1)"}, {"frameOrder", 20}
    };
    agora::agent::fields(item);

    auto bad_order = item;
    bad_order["frameOrder"] = -5;
    bool order_denied = false;
    try {
        agora::agent::fields(bad_order);
    } catch (const std::invalid_argument&) {
        order_denied = true;
    }
    AGORA_CHECK(order_denied);

    item["permission"] = "admin-group";
    bool denied = false;
    try {
        agora::agent::fields(item);
    } catch (const std::invalid_argument&) {
        denied = true;
    }
    AGORA_CHECK(denied);

    auto publicItem = agora::agent::publicItem(
        {{"kind", "text"}, {"renderMode", "irade-markdown"}, {"frameOrder", 10}, {"text", "source"}, {"token", "hidden"}, {"runtimeResults", Json::array()}},
        true);
    AGORA_CHECK(publicItem["text"] == "source" && publicItem["frameOrder"] == 10 && !publicItem.contains("token") && !publicItem.contains("runtimeResults"));
}

int main() {
    return runTest("agent Unix bridge, IRADE cell validation and source filtering", bridgeRoundTrip);
}

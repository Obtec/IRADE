#include "service/HealthService.hpp"

void HealthService::registerRoutes(HttpApi& api) {
    api.get("/health", [](const HttpApi::Request&, HttpApi::Response& res) {
        res.status = 200;
        res.set_content(R"({"status":"UP","service":"IRADE C++ Realtime Server"})", "application/json");
    });

    api.get("/", [](const HttpApi::Request&, HttpApi::Response& res) {
        res.status = 200;
        res.set_content(R"({"status":"online","service":"IRADE C++ Server"})", "application/json");
    });
}

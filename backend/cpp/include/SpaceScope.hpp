#pragma once

#include <cstddef>
#include <functional>
#include <limits>
#include <nlohmann/json.hpp>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>

enum class SpaceScope {
    Workspace
};

struct SpaceKey {
    SpaceScope scope{SpaceScope::Workspace};
    int id{0};

    bool operator==(const SpaceKey& other) const noexcept {
        return scope == other.scope && id == other.id;
    }
};

namespace std {
template <>
struct hash<SpaceKey> {
    std::size_t operator()(const SpaceKey& key) const noexcept {
        return (1ULL << 32) ^ std::hash<int>{}(key.id);
    }
};
}  // namespace std

inline constexpr std::string_view spaceScopeWire(SpaceScope) noexcept {
    return "workspace";
}

inline constexpr std::string_view spaceScopeIdField(SpaceScope) noexcept {
    return "workspace-id";
}

inline constexpr std::string_view spaceScopeNameField(SpaceScope) noexcept {
    return "workspace-name";
}

inline constexpr std::string_view spaceScopePasswordHashField(SpaceScope) noexcept {
    return "workspace-password-hash";
}

inline constexpr std::string_view spaceScopeJwtSubject(SpaceScope) noexcept {
    return "workspace-access";
}

inline constexpr std::string_view spaceScopeJwtIdClaim(SpaceScope) noexcept {
    return "workspaceId";
}

inline constexpr std::string_view spaceRedisKeyPrefix(SpaceScope) noexcept {
    return "workspace:";
}

inline constexpr std::string_view spaceSqlTable(SpaceScope) noexcept {
    return "workspace_info";
}

inline constexpr std::string_view spaceSqlIdColumn(SpaceScope) noexcept {
    return "workspace_id";
}

inline SpaceScope parseSpaceScope(std::string_view raw) {
    if (raw.empty() || raw == "workspace") return SpaceScope::Workspace;
    throw std::invalid_argument("Unsupported space scope");
}

inline constexpr int WORKSPACE_RUNTIME_TARGET_ID_OFFSET = 1000000000;

inline int runtimeTargetId(SpaceScope, int space_id) {
    if (space_id <= 0) return space_id;
    if (space_id > std::numeric_limits<int>::max() - WORKSPACE_RUNTIME_TARGET_ID_OFFSET) {
        throw std::out_of_range("Workspace runtime target ID overflow");
    }
    return WORKSPACE_RUNTIME_TARGET_ID_OFFSET + space_id;
}

inline std::string canonicalWorkspaceField(std::string_view field) {
    if (field == "canvas-id" || field == "canvasId" || field == "canvas_id"
            || field == "workspaceId" || field == "workspace_id") {
        return "workspace-id";
    }
    if (field == "canvas-name" || field == "canvasName" || field == "canvas_name"
            || field == "workspaceName" || field == "workspace_name") {
        return "workspace-name";
    }
    if (field == "canvas-password-hash" || field == "canvasPassword" || field == "canvas_password_hash"
            || field == "canvas-password" || field == "workspacePassword"
            || field == "workspace_password_hash" || field == "workspace-password") {
        return "workspace-password-hash";
    }
    return std::string(field);
}

inline int readWorkspaceId(const nlohmann::json& doc, int fallback = 0) {
    if (!doc.is_object()) return fallback;
    for (const char* key : {"workspace-id", "canvas-id", "workspaceId", "canvasId", "workspace_id", "canvas_id"}) {
        if (doc.contains(key) && doc[key].is_number_integer()) return doc[key].get<int>();
    }
    return fallback;
}

inline std::string readWorkspaceName(const nlohmann::json& doc, const std::string& fallback = "") {
    if (!doc.is_object()) return fallback;
    for (const char* key : {"workspace-name", "canvas-name", "workspaceName", "canvasName", "workspace_name", "canvas_name"}) {
        if (doc.contains(key) && doc[key].is_string()) {
            const auto value = doc[key].get<std::string>();
            if (!value.empty()) return value;
        }
    }
    return fallback;
}

inline std::optional<std::string> readWorkspacePasswordHash(const nlohmann::json& doc) {
    if (!doc.is_object()) return std::nullopt;
    if (doc.contains("workspace-password-hash")) {
        return doc["workspace-password-hash"].is_string()
            ? std::optional<std::string>(doc["workspace-password-hash"].get<std::string>())
            : std::nullopt;
    }
    if (doc.contains("canvas-password-hash") && doc["canvas-password-hash"].is_string()) {
        return doc["canvas-password-hash"].get<std::string>();
    }
    return std::nullopt;
}

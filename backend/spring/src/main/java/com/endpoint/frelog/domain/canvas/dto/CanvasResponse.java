package com.endpoint.frelog.domain.canvas.dto;

import com.endpoint.frelog.domain.canvas.entity.WorkspaceInfo;

import java.time.LocalDateTime;

public record CanvasResponse(
        Integer canvasId,
        String canvasName,
        Long userId,
        String userNickname,
        String redisIp,
        String redisPort,
        String serverIp,
        String serverPort,
        Boolean isCached,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
    public static CanvasResponse from(WorkspaceInfo entity) {
        return new CanvasResponse(
                entity.getWorkspaceId(),
                null,
                null,
                null,
                entity.getRedisIp(),
                entity.getRedisPort(),
                entity.getServerIp(),
                entity.getServerPort(),
                entity.getIsCached(),
                entity.getCreatedAt(),
                entity.getUpdatedAt()
        );
    }
}

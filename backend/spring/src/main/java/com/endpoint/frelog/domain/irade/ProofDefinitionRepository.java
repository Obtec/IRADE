package com.endpoint.frelog.domain.irade;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface ProofDefinitionRepository extends JpaRepository<ProofDefinition, Long> {

    Optional<ProofDefinition> findByWorkspaceKeyAndProverEngineAndSymbolName(
            String workspaceKey,
            String proverEngine,
            String symbolName
    );

    List<ProofDefinition> findByWorkspaceKeyOrderByUpdatedAtDescDefinitionIdDesc(String workspaceKey);

    List<ProofDefinition> findAllByOrderByUpdatedAtDescDefinitionIdDesc();
}

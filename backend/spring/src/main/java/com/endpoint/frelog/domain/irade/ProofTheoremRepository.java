package com.endpoint.frelog.domain.irade;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface ProofTheoremRepository extends JpaRepository<ProofTheorem, Long> {

    Optional<ProofTheorem> findByWorkspaceKeyAndProverEngineAndTheoremName(
            String workspaceKey,
            String proverEngine,
            String theoremName
    );

    List<ProofTheorem> findByWorkspaceKeyOrderByVerifiedAtDescTheoremIdDesc(String workspaceKey);

    List<ProofTheorem> findAllByOrderByVerifiedAtDescTheoremIdDesc();
}

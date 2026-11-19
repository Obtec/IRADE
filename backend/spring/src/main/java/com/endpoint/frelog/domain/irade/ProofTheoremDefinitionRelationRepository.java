package com.endpoint.frelog.domain.irade;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;

public interface ProofTheoremDefinitionRelationRepository extends JpaRepository<ProofTheoremDefinitionRelation, Long> {

    @Query("SELECT r FROM ProofTheoremDefinitionRelation r " +
            "JOIN FETCH r.theorem t " +
            "JOIN FETCH r.definition d " +
            "WHERE t.workspaceKey = :workspaceKey " +
            "ORDER BY r.relationId DESC")
    List<ProofTheoremDefinitionRelation> findByWorkspaceKey(@Param("workspaceKey") String workspaceKey);

    @Query("SELECT r FROM ProofTheoremDefinitionRelation r " +
            "JOIN FETCH r.theorem t " +
            "JOIN FETCH r.definition d " +
            "ORDER BY r.relationId DESC")
    List<ProofTheoremDefinitionRelation> findAllWithTargets();

    @Modifying(flushAutomatically = true)
    @Query("DELETE FROM ProofTheoremDefinitionRelation r WHERE r.theorem.theoremId = :theoremId")
    void deleteByTheoremId(@Param("theoremId") Long theoremId);
}

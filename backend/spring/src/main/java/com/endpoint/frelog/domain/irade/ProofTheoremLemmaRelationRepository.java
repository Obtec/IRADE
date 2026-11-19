package com.endpoint.frelog.domain.irade;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;

public interface ProofTheoremLemmaRelationRepository extends JpaRepository<ProofTheoremLemmaRelation, Long> {

    @Query("SELECT r FROM ProofTheoremLemmaRelation r " +
            "JOIN FETCH r.theorem t " +
            "LEFT JOIN FETCH r.lemmaTheorem lt " +
            "WHERE t.workspaceKey = :workspaceKey " +
            "ORDER BY r.relationId DESC")
    List<ProofTheoremLemmaRelation> findByWorkspaceKey(@Param("workspaceKey") String workspaceKey);

    @Query("SELECT r FROM ProofTheoremLemmaRelation r " +
            "JOIN FETCH r.theorem t " +
            "LEFT JOIN FETCH r.lemmaTheorem lt " +
            "ORDER BY r.relationId DESC")
    List<ProofTheoremLemmaRelation> findAllWithTheorems();

    @Query("SELECT r FROM ProofTheoremLemmaRelation r " +
            "WHERE r.lemmaTheorem IS NULL " +
            "AND r.theorem.workspaceKey = :workspaceKey " +
            "AND r.theorem.proverEngine = :proverEngine " +
            "AND r.lemmaName = :lemmaName")
    List<ProofTheoremLemmaRelation> findUnlinkedReferences(
            @Param("workspaceKey") String workspaceKey,
            @Param("proverEngine") String proverEngine,
            @Param("lemmaName") String lemmaName
    );

    @Modifying(flushAutomatically = true)
    @Query("DELETE FROM ProofTheoremLemmaRelation r WHERE r.theorem.theoremId = :theoremId")
    void deleteByTheoremId(@Param("theoremId") Long theoremId);

    @Modifying(flushAutomatically = true)
    @Query("UPDATE ProofTheoremLemmaRelation r SET r.lemmaTheorem = null WHERE r.lemmaTheorem.theoremId = :theoremId")
    void detachTargetTheorem(@Param("theoremId") Long theoremId);
}

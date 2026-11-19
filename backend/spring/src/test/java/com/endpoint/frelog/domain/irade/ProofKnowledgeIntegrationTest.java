package com.endpoint.frelog.domain.irade;

import com.endpoint.frelog.domain.canvas.client.CppServerClient;
import com.endpoint.frelog.domain.canvas.service.CanvasRedisDocumentReader;
import com.zaxxer.hikari.HikariDataSource;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.convention.TestBean;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.web.context.WebApplicationContext;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

import javax.sql.DataSource;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.security.test.web.servlet.setup.SecurityMockMvcConfigurers.springSecurity;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@SpringBootTest
class ProofKnowledgeIntegrationTest {

    @MockitoBean
    private CppServerClient cppServerClient;

    @MockitoBean
    private CanvasRedisDocumentReader canvasRedisDocumentReader;

    @TestBean(methodName = "database")
    private DataSource dataSource;

    static DataSource database() {
        var db = new HikariDataSource();
        db.setJdbcUrl("jdbc:h2:mem:proof_knowledge_test;MODE=MSSQLServer;DB_CLOSE_DELAY=-1");
        db.setUsername("sa");
        return db;
    }

    @Autowired
    private WebApplicationContext context;

    @Autowired
    private ObjectMapper objectMapper;

    @Autowired
    private ProofTheoremRepository theoremRepository;

    @Autowired
    private ProofDefinitionRepository definitionRepository;

    @Autowired
    private ProofTheoremLemmaRelationRepository lemmaRelationRepository;

    @Autowired
    private ProofTheoremDefinitionRelationRepository definitionRelationRepository;

    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        mockMvc = MockMvcBuilders.webAppContextSetup(context).apply(springSecurity()).build();
        definitionRelationRepository.deleteAll();
        lemmaRelationRepository.deleteAll();
        theoremRepository.deleteAll();
        definitionRepository.deleteAll();
    }

    @Test
    void rejectsNonVerifiedOrIncompleteProofs() throws Exception {
        String failedPayload = objectMapper.writeValueAsString(Map.of(
                "workspaceKey", "ws-reject",
                "proverEngine", "LEAN4",
                "verificationStatus", "ERROR",
                "sourceCode", "theorem fail_thm : 1 = 1 := rfl"
        ));

        mockMvc.perform(post("/api/irade/proofs/verified")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(failedPayload))
                .andExpect(status().isBadRequest());

        String sorryPayload = objectMapper.writeValueAsString(Map.of(
                "workspaceKey", "ws-reject",
                "proverEngine", "LEAN4",
                "verificationStatus", "VERIFIED",
                "sourceCode", "theorem unfinished : 1 = 1 := by\n  sorry"
        ));

        mockMvc.perform(post("/api/irade/proofs/verified")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(sorryPayload))
                .andExpect(status().isBadRequest());

        assertThat(theoremRepository.count()).isZero();
        assertThat(definitionRepository.count()).isZero();
    }

    @Test
    void persistsVerifiedTheoremsLemmaRelationsAndDefinitionsInRelationalTables() throws Exception {
        String leanCode = """
                def idNat (n : Nat) : Nat := n

                lemma add_zero_lemma (n : Nat) : n + 0 = n := by
                  rfl

                theorem add_zero_identity (n : Nat) : idNat n + 0 = n := by
                  simpa [idNat] using add_zero_lemma n

                #check add_zero_identity
                """;

        String payload = objectMapper.writeValueAsString(Map.of(
                "workspaceId", 12,
                "workspaceKey", "ws-lean-rel",
                "proverEngine", "LEAN4",
                "verificationStatus", "VERIFIED",
                "cellId", "cell-lean-1",
                "sourceCode", leanCode,
                "verifierOutput", "add_zero_identity (n : Nat) : idNat n + 0 = n"
        ));

        String responseBody = mockMvc.perform(post("/api/irade/proofs/verified")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(payload))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.workspaceKey").value("ws-lean-rel"))
                .andExpect(jsonPath("$.definitions.length()").value(1))
                .andExpect(jsonPath("$.definitions[0].symbolName").value("idNat"))
                .andExpect(jsonPath("$.theorems.length()").value(2))
                .andExpect(jsonPath("$.lemmaRelations.length()").value(1))
                .andExpect(jsonPath("$.lemmaRelations[0].theoremName").value("add_zero_identity"))
                .andExpect(jsonPath("$.lemmaRelations[0].lemmaName").value("add_zero_lemma"))
                .andExpect(jsonPath("$.lemmaRelations[0].lemmaVerifiedInDb").value(true))
                .andExpect(jsonPath("$.definitionRelations.length()").value(1))
                .andExpect(jsonPath("$.definitionRelations[0].theoremName").value("add_zero_identity"))
                .andExpect(jsonPath("$.definitionRelations[0].definitionName").value("idNat"))
                .andExpect(jsonPath("$.definitionRelations[0].usageRole").value("BOTH"))
                .andReturn()
                .getResponse()
                .getContentAsString();

        JsonNode tree = objectMapper.readTree(responseBody);
        long lemmaId = -1L;
        for (JsonNode thm : tree.get("theorems")) {
            if ("add_zero_lemma".equals(thm.get("theoremName").asText())) {
                lemmaId = thm.get("theoremId").asLong();
            }
        }
        assertThat(lemmaId).isPositive();

        // Deleting a referenced lemma detaches incoming FKs rather than failing constraint integrity.
        mockMvc.perform(delete("/api/irade/proofs/theorems/" + lemmaId))
                .andExpect(status().isOk());

        mockMvc.perform(get("/api/irade/proofs").param("workspaceKey", "ws-lean-rel"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.theorems.length()").value(1))
                .andExpect(jsonPath("$.theorems[0].theoremName").value("add_zero_identity"))
                .andExpect(jsonPath("$.lemmaRelations[0].lemmaVerifiedInDb").value(false));
    }

    @Test
    void persistsVerifiedRocqProofsAndBackfillsOutOfOrderLemmaReferences() throws Exception {
        String thmFirst = objectMapper.writeValueAsString(Map.of(
                "workspaceKey", "ws-rocq-rel",
                "proverEngine", "ROCQ",
                "verificationStatus", "VERIFIED",
                "cellId", "cell-rocq-1",
                "sourceCode", """
                        Definition id_nat (n : nat) : nat := n.
                        Theorem main_identity : forall n : nat, id_nat n + 0 = n.
                        Proof.
                          intros n. unfold id_nat. induction n; simpl; auto.
                        Qed.
                        """,
                "verifierOutput", "Rocq verification passed",
                "lemmaRelations", List.of(Map.of(
                        "theoremName", "main_identity",
                        "lemmaName", "helper_zero",
                        "relationType", "APPLIES",
                        "referenceContext", "apply helper_zero"
                ))
        ));

        mockMvc.perform(post("/api/irade/proofs/verified")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(thmFirst))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.lemmaRelations[0].lemmaVerifiedInDb").value(false));

        String lemmaLater = objectMapper.writeValueAsString(Map.of(
                "workspaceKey", "ws-rocq-rel",
                "proverEngine", "ROCQ",
                "verificationStatus", "VERIFIED",
                "cellId", "cell-rocq-2",
                "sourceCode", """
                        Lemma helper_zero : forall n : nat, n + 0 = n.
                        Proof.
                          intros n. induction n; simpl; auto.
                        Qed.
                        """,
                "verifierOutput", "Rocq verification passed"
        ));

        mockMvc.perform(post("/api/irade/proofs/verified")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(lemmaLater))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.theorems.length()").value(2))
                .andExpect(jsonPath("$.lemmaRelations[0].lemmaName").value("helper_zero"))
                .andExpect(jsonPath("$.lemmaRelations[0].lemmaVerifiedInDb").value(true));
    }
}

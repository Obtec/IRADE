package com.endpoint.frelog.domain.canvas.service;

import com.endpoint.frelog.domain.canvas.dto.CanvasDocument;
import com.endpoint.frelog.domain.canvas.dto.CanvasSummaryResponse;
import com.endpoint.frelog.domain.canvas.dto.CanvasUpdateDtos;
import com.endpoint.frelog.domain.canvas.dto.DocumentScope;
import com.endpoint.frelog.domain.canvas.entity.WorkspaceInfo;
import com.endpoint.frelog.domain.canvas.repository.WorkspaceInfoRepository;
import com.endpoint.frelog.domain.loadbalancer.dto.AllocateRedisResponse;
import com.endpoint.frelog.domain.loadbalancer.dto.AllocateServerResponse;
import com.endpoint.frelog.domain.loadbalancer.service.LoadBalancerService;
import com.endpoint.frelog.domain.user.entity.Role;
import com.endpoint.frelog.domain.user.entity.User;
import com.endpoint.frelog.domain.user.entity.UserStatus;
import com.endpoint.frelog.domain.user.repository.UserRepository;
import com.endpoint.frelog.global.exception.CustomException;
import com.endpoint.frelog.global.exception.ErrorCode;
import com.endpoint.frelog.global.security.CustomUserDetails;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.test.util.ReflectionTestUtils;

import java.util.List;
import java.util.Optional;
import java.util.Map;
import org.mockito.ArgumentCaptor;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;

@ExtendWith(MockitoExtension.class)
class CanvasServiceTest {

    @Mock
    private WorkspaceInfoRepository workspaceInfoRepository;

    @Mock
    private UserRepository userRepository;

    @Mock
    private com.endpoint.frelog.domain.user.repository.UserSessionRepository userSessionRepository;

    @Mock
    private CanvasElasticsearchService canvasElasticsearchService;

    @Mock
    private CanvasRedisDocumentReader redisDocumentReader;

    @Mock
    private CanvasResourceService canvasResourceService;

    @Mock
    private LoadBalancerService loadBalancerService;

    @Mock
    private com.endpoint.frelog.domain.loadbalancer.repository.ServerInfoRepository serverInfoRepository;

    @Mock
    private com.endpoint.frelog.domain.loadbalancer.repository.RedisInfoRepository redisInfoRepository;

    @Mock
    private com.endpoint.frelog.global.security.JwtTokenProvider jwtTokenProvider;

    @Mock private com.endpoint.frelog.domain.canvas.runtime.RuntimeCleanupQueue runtimeCleanupQueue;
    @Mock private org.springframework.context.ApplicationEventPublisher events;
    @InjectMocks
    private CanvasService canvasService;

    private User testUser;
    private CustomUserDetails userDetails;

    @BeforeEach
    void setUp() {
        testUser = new User("user@irade.com", "encodedPassword", "IRADE유저", 1, Role.ROLE_USER);
        testUser.setStatus(UserStatus.ACTIVE);
        ReflectionTestUtils.setField(testUser, "userId", 1L);
        userDetails = new CustomUserDetails(testUser);
    }

    @Test
    @DisplayName("캔버스 생성 시 MS SQL과 Elasticsearch에 각각 데이터가 저장된다")
    void createCanvas_Success() {
        // given
        WorkspaceInfo savedInfo = new WorkspaceInfo(101);
        given(workspaceInfoRepository.save(any(WorkspaceInfo.class))).willReturn(savedInfo);
        given(canvasResourceService.saveRepresentativeImage(eq(DocumentScope.WORKSPACE), eq(101), any())).willReturn("/api/workspaces/101/image");
        given(canvasElasticsearchService.saveDocument(eq(DocumentScope.WORKSPACE), any(CanvasDocument.class))).willReturn(true);

        // when
        CanvasSummaryResponse response = canvasService.createCanvas("Test Canvas", "description", "pass123", null, userDetails, "public");

        // then
        assertThat(response).isNotNull();
        assertThat(response.canvasId()).isEqualTo(101);
        assertThat(response.canvasName()).isEqualTo("Test Canvas");
        assertThat(response.description()).isEqualTo("description");

        verify(workspaceInfoRepository).save(any(WorkspaceInfo.class));
        ArgumentCaptor<CanvasDocument> saved = ArgumentCaptor.forClass(CanvasDocument.class);
        verify(canvasElasticsearchService).saveDocument(eq(DocumentScope.WORKSPACE), saved.capture());
        assertThat(saved.getValue().getCanvasPasswordHash()).startsWith("$2").isNotEqualTo("pass123");
    }

    @Test
    void privateCanvasRejectsPasswordAtCreationIncludingLegacyDefault() {
        for (String visibility : new String[]{"private", null}) {
            assertThatThrownBy(() -> canvasService.createCanvas("Private", "", "secret", null, userDetails, visibility))
                    .isInstanceOf(CustomException.class).hasFieldOrPropertyWithValue("errorCode", ErrorCode.INVALID_INPUT_VALUE);
        }
        verifyNoInteractions(workspaceInfoRepository, canvasElasticsearchService);
    }

    @Test
    void passwordUpdateOnlyAllowedForPublicCanvas() {
        CanvasDocument doc = new CanvasDocument("Canvas", 11, 1L, null, "default");
        given(workspaceInfoRepository.findByIdWithPessimisticLock(11)).willReturn(Optional.of(new WorkspaceInfo(11)));
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 11)).willReturn(Optional.of(doc));
        assertThatThrownBy(() -> canvasService.updateCanvasPassword(11, "secret", userDetails))
                .isInstanceOf(CustomException.class).hasFieldOrPropertyWithValue("errorCode", ErrorCode.INVALID_INPUT_VALUE);
        doc.setVisibility("public");
        canvasService.updateCanvasPassword(11, "secret", userDetails);
        ArgumentCaptor<Map> fields = ArgumentCaptor.forClass(Map.class);
        verify(canvasElasticsearchService).patchFields(eq(DocumentScope.WORKSPACE), eq(11), fields.capture());
        assertThat(CanvasPasswords.matches("secret", (String) fields.getValue().get("workspace-password-hash"))).isTrue();
    }

    @Test
    void privateMembershipIgnoresLegacyPasswordAndRevokedInvitationCannotReenter() {
        CanvasDocument doc = new CanvasDocument("Private", 77, 2L, "old-hash", "default");
        doc.setPeople(new java.util.ArrayList<>(List.of(2L, 1L)));
        doc.getInnerGroup().get("default").add(1L);
        assertThat(CanvasVisibility.allows(doc, userDetails)).isTrue();
        doc.getPeople().remove(1L);
        assertThat(CanvasVisibility.allows(doc, userDetails)).isFalse();
        doc.setVisibility("public");
        assertThat(CanvasVisibility.allows(doc, userDetails)).isTrue();
    }

    @Test
    void adminCanRemoveInvitedParticipantFromAllGroupsButCannotRemoveOwner() {
        CanvasDocument doc = new CanvasDocument("Private", 11, 1L, null, "default");
        doc.setPeople(new java.util.ArrayList<>(List.of(1L, 2L)));
        doc.getInnerGroup().put("default", new java.util.ArrayList<>(List.of(2L)));
        given(workspaceInfoRepository.findByIdWithPessimisticLock(11)).willReturn(Optional.of(new WorkspaceInfo(11)));
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 11)).willReturn(Optional.of(doc));
        User target = new User("other@irade.com", "hash", "invited", 7, Role.ROLE_USER);
        ReflectionTestUtils.setField(target, "userId", 2L);
        given(userRepository.findByNicknameAndTagNumber("invited", 7)).willReturn(Optional.of(target));
        canvasService.removeCanvasParticipant(11, "invited", 7, userDetails);
        ArgumentCaptor<Map> fields = ArgumentCaptor.forClass(Map.class);
        verify(canvasElasticsearchService).patchFields(eq(DocumentScope.WORKSPACE), eq(11), fields.capture());
        assertThat((List<Long>) fields.getValue().get("people")).containsExactly(1L);
        assertThat(((Map<String,List<Long>>) fields.getValue().get("inner-group")).values()).allSatisfy(members -> assertThat(members).doesNotContain(2L));
        given(userRepository.findByNicknameAndTagNumber(testUser.getNickname(), 1)).willReturn(Optional.of(testUser));
        assertThatThrownBy(() -> canvasService.removeCanvasParticipant(11, testUser.getNickname(), 1, userDetails))
                .isInstanceOf(CustomException.class).hasFieldOrPropertyWithValue("errorCode", ErrorCode.BAD_REQUEST);
    }

    @Test
    @DisplayName("참여자는 닉네임과 태그로 찾아 초기 그룹에 추가한다")
    void addParticipant_UsesHandleAndInitialGroup() {
        CanvasDocument doc = new CanvasDocument("Canvas", 11, 1L, null, "default");
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 11)).willReturn(Optional.of(doc));
        given(workspaceInfoRepository.findByIdWithPessimisticLock(11)).willReturn(Optional.of(new WorkspaceInfo(11)));
        User participant = new User("other@irade.com", "hash", "동료", 7, Role.ROLE_USER);
        participant.setStatus(UserStatus.ACTIVE);
        ReflectionTestUtils.setField(participant, "userId", 2L);
        given(userRepository.findByNicknameAndTagNumber("동료", 7)).willReturn(Optional.of(participant));

        canvasService.addCanvasParticipant(11, "동료", 7, userDetails);

        ArgumentCaptor<Map> fields = ArgumentCaptor.forClass(Map.class);
        verify(canvasElasticsearchService).patchFields(eq(DocumentScope.WORKSPACE), eq(11), fields.capture());
        assertThat((List<Long>) fields.getValue().get("people")).containsExactly(1L, 2L);
        assertThat((Map<String, List<Long>>) fields.getValue().get("inner-group"))
                .containsEntry("default", List.of(2L));
    }

    @Test
    @DisplayName("활성 캔버스의 설정 변경은 캐시 덮어쓰기를 막기 위해 거부한다")
    void updateName_CachedCanvasRejected() {
        WorkspaceInfo info = new WorkspaceInfo(12);
        info.setIsCached(true);
        given(workspaceInfoRepository.findByIdWithPessimisticLock(12)).willReturn(Optional.of(info));

        assertThatThrownBy(() -> canvasService.updateCanvasName(12, "Changed", userDetails))
                .isInstanceOf(CustomException.class)
                .hasFieldOrPropertyWithValue("errorCode", ErrorCode.WORKSPACE_ACTIVE);
    }

    @Test
    @DisplayName("보호된 캔버스는 비밀번호 없이는 접속 토큰을 발급하지 않는다")
    void accessCanvas_RequiresPassword() {
        CanvasDocument doc = new CanvasDocument("Protected", 13, 1L, "secret", "default");
        doc.setVisibility("public");
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 13)).willReturn(Optional.of(doc));
        given(workspaceInfoRepository.findByIdWithPessimisticLock(13)).willReturn(Optional.of(new WorkspaceInfo(13)));
        jakarta.servlet.http.HttpServletRequest request = org.mockito.Mockito.mock(jakarta.servlet.http.HttpServletRequest.class);

        assertThatThrownBy(() -> canvasService.accessCanvas(13, request, userDetails))
                .isInstanceOf(CustomException.class)
                .hasFieldOrPropertyWithValue("errorCode", ErrorCode.WORKSPACE_PASSWORD_REQUIRED);
        assertThatThrownBy(() -> canvasService.accessCanvas(13, request, userDetails, "wrong"))
                .isInstanceOf(CustomException.class)
                .hasFieldOrPropertyWithValue("errorCode", ErrorCode.WORKSPACE_PASSWORD_INVALID);
    }

    @Test
    @DisplayName("활성 캔버스 접속 권한은 Elasticsearch 사본이 아닌 Redis 최신 문서로 확인한다")
    void accessCanvas_ActiveUsesRedisDocument() {
        WorkspaceInfo info = new WorkspaceInfo(14);
        info.setIsCached(true);
        info.setRedisInfo(new com.endpoint.frelog.domain.loadbalancer.entity.RedisInfo("127.0.0.1", "6379"));
        given(workspaceInfoRepository.findByIdWithPessimisticLock(14)).willReturn(Optional.of(info));
        CanvasDocument live = new CanvasDocument("Live", 14, 1L, CanvasPasswords.hashForStorage("new-secret"), "default");
        live.setVisibility("public");
        given(redisDocumentReader.read(DocumentScope.WORKSPACE, info)).willReturn(live);
        jakarta.servlet.http.HttpServletRequest request = org.mockito.Mockito.mock(jakarta.servlet.http.HttpServletRequest.class);

        assertThatThrownBy(() -> canvasService.accessCanvas(14, request, userDetails, "old-secret"))
                .isInstanceOf(CustomException.class)
                .hasFieldOrPropertyWithValue("errorCode", ErrorCode.WORKSPACE_PASSWORD_INVALID);
        verifyNoInteractions(canvasElasticsearchService);
    }

    @Test
    @DisplayName("캔버스 검색 시 Elasticsearch에서 조회하고 CanvasSummaryResponse 목록 반환")
    void searchCanvases_Success() {
        // given
        CanvasDocument doc = new CanvasDocument("IRADE Workspace", 50, 1L, null, "default");
        doc.setDescription("IRADE workspace desc");
        doc.setPeople(List.of(1L, 2L));

        given(canvasElasticsearchService.searchDocumentsByName(DocumentScope.WORKSPACE, "IRADE")).willReturn(List.of(doc));
        given(workspaceInfoRepository.findById(50)).willReturn(Optional.of(new WorkspaceInfo(50)));

        // when
        List<CanvasSummaryResponse> results = canvasService.searchCanvases("IRADE", userDetails);

        // then
        assertThat(results).hasSize(1);
        CanvasSummaryResponse summary = results.get(0);
        assertThat(summary.canvasId()).isEqualTo(50);
        assertThat(summary.canvasName()).isEqualTo("IRADE Workspace");
        assertThat(summary.description()).isEqualTo("IRADE workspace desc");
        assertThat(summary.userCount()).isEqualTo(2);
    }

    @Test
    @DisplayName("캔버스 단건 요약 조회 성공")
    void getCanvasSummary_Success() {
        // given
        CanvasDocument doc = new CanvasDocument("Solo Canvas", 10, 1L, null, "default");
        doc.setDescription("Solo desc");
        doc.setPeople(List.of(1L));

        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 10)).willReturn(Optional.of(doc));
        given(workspaceInfoRepository.findByIdWithPessimisticLock(10)).willReturn(Optional.of(new WorkspaceInfo(10)));

        // when
        CanvasSummaryResponse summary = canvasService.getCanvasSummary(10, userDetails);

        // then
        assertThat(summary).isNotNull();
        assertThat(summary.canvasId()).isEqualTo(10);
        assertThat(summary.canvasName()).isEqualTo("Solo Canvas");
        assertThat(summary.userCount()).isEqualTo(1);
    }

    @Test
    @DisplayName("비공개 캔버스는 초대받지 않은 사용자에게 검색이나 상세 조회로 노출되지 않는다")
    void privateCanvas_IsHiddenFromNonParticipants() {
        CanvasDocument hidden = new CanvasDocument("Private", 77, 2L, null, "default");
        given(canvasElasticsearchService.searchDocumentsByName(DocumentScope.WORKSPACE, "Private")).willReturn(List.of(hidden));
        given(workspaceInfoRepository.findByIdWithPessimisticLock(77)).willReturn(Optional.of(new WorkspaceInfo(77)));
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 77)).willReturn(Optional.of(hidden));
        assertThat(canvasService.searchCanvases("Private", userDetails)).isEmpty();
        assertThatThrownBy(() -> canvasService.getCanvasSummary(77, userDetails))
                .isInstanceOf(CustomException.class).hasFieldOrPropertyWithValue("errorCode", ErrorCode.WORKSPACE_NOT_FOUND);
        assertThatThrownBy(() -> canvasService.accessCanvas(77, org.mockito.Mockito.mock(jakarta.servlet.http.HttpServletRequest.class), userDetails))
                .isInstanceOf(CustomException.class).hasFieldOrPropertyWithValue("errorCode", ErrorCode.WORKSPACE_NOT_FOUND);
    }

    @Test
    @DisplayName("공개 캔버스는 로그인한 비참가자도 검색과 상세 조회를 할 수 있다")
    void publicCanvas_IsVisibleToSignedInUsers() {
        CanvasDocument doc = new CanvasDocument("Public", 78, 2L, null, "default"); doc.setVisibility("public");
        given(canvasElasticsearchService.searchDocumentsByName(DocumentScope.WORKSPACE, "Public")).willReturn(List.of(doc));
        given(workspaceInfoRepository.findById(78)).willReturn(Optional.of(new WorkspaceInfo(78)));
        given(workspaceInfoRepository.findByIdWithPessimisticLock(78)).willReturn(Optional.of(new WorkspaceInfo(78)));
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 78)).willReturn(Optional.of(doc));
        assertThat(canvasService.searchCanvases("Public", userDetails)).extracting(CanvasSummaryResponse::visibility).containsExactly("public");
        assertThat(canvasService.getCanvasSummary(78, userDetails).visibility()).isEqualTo("public");
    }

    @Test
    @DisplayName("활성 캔버스 검색은 Redis의 최신 비공개 설정을 확인한다")
    void searchUsesActiveVisibility() {
        CanvasDocument stale = new CanvasDocument("Changed", 79, 2L, null, "default"); stale.setVisibility("public");
        CanvasDocument live = new CanvasDocument("Changed", 79, 2L, null, "default");
        WorkspaceInfo info = new WorkspaceInfo(79); info.setIsCached(true);
        given(canvasElasticsearchService.searchDocumentsByName(DocumentScope.WORKSPACE, "Changed")).willReturn(List.of(stale));
        given(workspaceInfoRepository.findById(79)).willReturn(Optional.of(info));
        given(redisDocumentReader.read(DocumentScope.WORKSPACE, info)).willReturn(live);
        assertThat(canvasService.searchCanvases("Changed", userDetails)).isEmpty();
    }



    @Test
    @DisplayName("캔버스 삭제 시 is_cached가 true라면 CustomException(BAD_REQUEST)을 던진다")
    void deleteCanvas_Cached_ThrowsException() {
        // given
        CanvasDocument doc = new CanvasDocument("Delete Canvas", 200, 1L, null, "default");
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 200)).willReturn(Optional.of(doc));

        WorkspaceInfo info = new WorkspaceInfo(200);
        info.setIsCached(true);
        given(workspaceInfoRepository.findByIdWithPessimisticLock(200)).willReturn(Optional.of(info));

        // when & then
        assertThatThrownBy(() -> canvasService.deleteCanvas(200, userDetails))
                .isInstanceOf(CustomException.class)
                .hasFieldOrPropertyWithValue("errorCode", ErrorCode.WORKSPACE_ACTIVE);
    }

    @Test void deleteCanvasQueuesCleanupAndPublishesOnlyAfterDocumentDeletion() {
        var doc = new CanvasDocument("Delete",201,1L,null,"default");
        var info = new WorkspaceInfo(201);
        given(workspaceInfoRepository.findByIdWithPessimisticLock(201)).willReturn(Optional.of(info));
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 201)).willReturn(Optional.of(doc));
        given(canvasElasticsearchService.deleteDocument(DocumentScope.WORKSPACE, 201,"Delete")).willReturn(true);
        canvasService.deleteCanvas(201,userDetails);
        var order=org.mockito.Mockito.inOrder(runtimeCleanupQueue,workspaceInfoRepository,canvasElasticsearchService,events);
        order.verify(runtimeCleanupQueue).enqueue(DocumentScope.WORKSPACE, 201);
        order.verify(workspaceInfoRepository).delete(info);
        order.verify(canvasElasticsearchService).deleteDocument(DocumentScope.WORKSPACE, 201,"Delete");
        order.verify(events).publishEvent(new com.endpoint.frelog.domain.canvas.runtime.RuntimeCleanupCoordinator.DeletedSpace(DocumentScope.WORKSPACE, 201));
    }

    @Test void failedDocumentDeleteDoesNotPublishCleanupEvent() {
        var doc = new CanvasDocument("Delete",202,1L,null,"default");
        given(workspaceInfoRepository.findByIdWithPessimisticLock(202)).willReturn(Optional.of(new WorkspaceInfo(202)));
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 202)).willReturn(Optional.of(doc));
        assertThatThrownBy(()->canvasService.deleteCanvas(202,userDetails)).isInstanceOf(CustomException.class);
        verifyNoInteractions(events);
    }

    @Test
    @DisplayName("캔버스 접속 시 참여자 목록과 무관하게 미캐시 상태면 P2C로 할당하고 JWT를 C++ 서버에 등록한다")
    void accessCanvas_NotCached_AllocatesAndRegisters() {
        // given
        CanvasDocument doc = new CanvasDocument("Access Canvas", 300, 1L, "hashedPass", "default");
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 300)).willReturn(Optional.of(doc));

        WorkspaceInfo info = new WorkspaceInfo(300);
        info.setIsCached(false);
        given(workspaceInfoRepository.findByIdWithPessimisticLock(300)).willReturn(Optional.of(info));

        given(loadBalancerService.allocateServer()).willReturn(AllocateServerResponse.of("127.0.0.1", "8000", "8002"));

        com.endpoint.frelog.domain.loadbalancer.entity.ServerInfo sInfo = new com.endpoint.frelog.domain.loadbalancer.entity.ServerInfo("127.0.0.1", "8000", "8002", "Cpp-1");
        sInfo.setServerId(1);
        given(serverInfoRepository.findByServerIpAndServerPort("127.0.0.1", "8000")).willReturn(java.util.Optional.of(sInfo));

        jakarta.servlet.http.HttpServletRequest request = org.mockito.Mockito.mock(jakarta.servlet.http.HttpServletRequest.class);
        given(request.getRemoteAddr()).willReturn("192.168.0.100");
        
        given(jwtTokenProvider.createWorkspaceAccessToken(
                anyString(), anyInt(), eq(300), eq("192.168.0.100"), anyString(), eq(0L)
        )).willReturn("mock-canvas-token");

        // when
        CanvasUpdateDtos.AccessResponse response = canvasService.accessCanvas(300, request, userDetails, "hashedPass");

        // then
        assertThat(response).isNotNull();
        assertThat(response.serverId()).isEqualTo(1);
        assertThat(response.wsPort()).isEqualTo("8002");
        assertThat(response.canvasAccessToken()).isEqualTo("mock-canvas-token");
        verifyNoInteractions(userSessionRepository);
        // assertThat(info.getIsCached()).isTrue();
        // assertThat(testUser.getIsAccessed()).isTrue();
    }

    @Test
    void branchCanvasCopiesAclButNotObjectsOrRuntimeState() {
        var parent = new CanvasDocument("Parent", 11, 1L, null, "default");
        parent.setPeople(new java.util.ArrayList<>(List.of(1L,2L)));
        parent.setInnerGroup(new java.util.LinkedHashMap<>(Map.of("admin-group", List.of(1L), "default", List.of(2L))));
        parent.setItems(Map.of("source-only", Map.of("kind","shape")));
        var child = new CanvasDocument("Child", 101, 1L, null, "default");
        given(workspaceInfoRepository.findByIdWithPessimisticLock(11)).willReturn(Optional.of(new WorkspaceInfo(11)));
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 11)).willReturn(Optional.of(parent));
        given(workspaceInfoRepository.save(any(WorkspaceInfo.class))).willReturn(new WorkspaceInfo(101));
        given(canvasResourceService.saveRepresentativeImage(eq(DocumentScope.WORKSPACE), eq(101), any())).willReturn("/api/workspaces/101/image");
        given(canvasElasticsearchService.saveDocument(eq(DocumentScope.WORKSPACE), any(CanvasDocument.class))).willReturn(true);
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 101)).willReturn(Optional.of(child));
        var result=canvasService.createBranchCanvas(11,"feature",userDetails);
        assertThat(result.canvasId()).isEqualTo(101);
        assertThat(child.getPeople()).containsExactly(1L,2L);
        assertThat(child.getInnerGroup()).isEqualTo(parent.getInnerGroup());
        assertThat(child.getInnerGroup()).isNotSameAs(parent.getInnerGroup());
        assertThat(child.getItems()).isEmpty();
        assertThat(child.getVisibility()).isEqualTo("private");
    }
    @Test
    void publicBranchInheritsThePasswordHashAndRevision() {
        var parent = new CanvasDocument("Parent",11,1L,CanvasPasswords.hashForStorage("secret"),"default");parent.setVisibility("public");parent.setPasswordRevision(7L);
        var child = new CanvasDocument("Child",101,1L,null,"default");
        given(workspaceInfoRepository.findByIdWithPessimisticLock(11)).willReturn(Optional.of(new WorkspaceInfo(11)));
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 11)).willReturn(Optional.of(parent));
        given(workspaceInfoRepository.save(any(WorkspaceInfo.class))).willReturn(new WorkspaceInfo(101));
        given(canvasResourceService.saveRepresentativeImage(eq(DocumentScope.WORKSPACE), eq(101),any())).willReturn("image");
        given(canvasElasticsearchService.saveDocument(eq(DocumentScope.WORKSPACE), any(CanvasDocument.class))).willReturn(true);
        given(canvasElasticsearchService.getDocumentById(DocumentScope.WORKSPACE, 101)).willReturn(Optional.of(child));
        canvasService.createBranchCanvas(11,"feature",userDetails);
        assertThat(child.getVisibility()).isEqualTo("public");assertThat(child.getCanvasPasswordHash()).isEqualTo(parent.getCanvasPasswordHash());assertThat(child.getPasswordRevision()).isEqualTo(7L);
    }
}

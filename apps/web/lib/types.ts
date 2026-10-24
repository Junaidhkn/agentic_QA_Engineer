import type { components } from "@re/api-client";

type S = components["schemas"];
export type Me = S["MeOut"];
export type Org = S["OrgOut"];
export type Member = S["MemberOut"];
export type Project = S["ProjectOut"];
export type Target = S["TargetOut"];
export type SecretMeta = S["SecretMeta"];
export type ApiKey = S["ApiKeyOut"];
export type ApiKeyCreated = S["ApiKeyCreated"];
export type Repo = S["RepoOut"];
export type LinkedRepo = S["LinkedRepo"];
export type Usage = S["UsageOut"];
export type AuditPage = S["AuditPage"];
export type Workflow = S["WorkflowOut"];
export type IrCheck = S["IrCheck"];
export type Suite = S["SuiteOut"];
export type SuiteCase = S["CaseOut"];
export type Profile = S["ProfileOut"];
export type Run = S["RunOut"];
export type RunSummaryRow = S["MetricSummaryOut"];
export type RunArtifact = S["ArtifactOut"];
export type Approval = S["ApprovalOut"];
export type DownloadUrl = S["DownloadUrl"];
export type Finding = S["FindingOut"];
export type EvidenceItem = S["EvidenceOut"];
export type IngestToken = S["IngestTokenOut"];
export type IngestTokenCreated = S["IngestTokenCreated"];
export type Discovery = S["DiscoveryOut"];
export type OpenApiSpec = S["OpenAPIOut"];
export type SourceArchive = S["ArchiveOut"];
export type SandboxBuild = S["BuildOut"];
export type Diagnosis = S["DiagnosisOut"];
export type Hypothesis = S["HypothesisOut"];
export type Citation = S["CitationOut"];
export interface CoverageCounts { discovered?: number; approved?: number; tested?: number; passing?: number }
export interface Coverage {
  totals: Required<CoverageCounts>;
  by_source: Record<string, CoverageCounts>;
  by_criticality: Record<string, CoverageCounts>;
  suite_version: number | null;
}
export interface SeriesPoint { t: number; vus: number; rps: number; error_rate: number; p50_ms: number | null; p95_ms: number | null; p99_ms: number | null }
export interface RunMetrics { scope: string; scopes: string[]; points: SeriesPoint[]; steady_state: { window_s: [number, number] } | null }

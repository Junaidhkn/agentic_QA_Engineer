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

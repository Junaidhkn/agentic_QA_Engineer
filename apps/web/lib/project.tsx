"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";

import { apiFetch } from "@/lib/api";
import { useOrg } from "@/lib/org";
import type { Project } from "@/lib/types";

export function useProject() {
  const { id } = useParams<{ id: string }>();
  const { orgs } = useOrg();
  const query = useQuery({ queryKey: ["project", id], queryFn: () => apiFetch<Project>(`/v1/projects/${id}`) });
  const role = orgs.find((o) => o.id === query.data?.org_id)?.role;
  return { id, project: query.data, query, role };
}

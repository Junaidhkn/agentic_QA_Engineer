import { apiFetch } from "@/lib/api";
import type { DownloadUrl } from "@/lib/types";

export async function openArtifact(artifactId: string) {
  const { url } = await apiFetch<DownloadUrl>(`/v1/artifacts/${artifactId}/download`);
  window.open(url, "_blank", "noopener");
}

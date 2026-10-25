import { posts } from "../../../../lib/db";

export async function GET(_req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const post = posts.find((p) => p.id === Number(id));
  return post ? Response.json(post) : Response.json({ error: "not found" }, { status: 404 });
}

export async function DELETE(_req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const i = posts.findIndex((p) => p.id === Number(id));
  if (i >= 0) posts.splice(i, 1);
  return new Response(null, { status: 204 });
}

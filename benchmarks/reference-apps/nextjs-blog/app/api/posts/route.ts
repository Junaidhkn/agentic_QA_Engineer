import { posts, nextId } from "../../../lib/db";

export async function GET() {
  return Response.json(posts);
}

export async function POST(request: Request) {
  const { title, body } = await request.json();
  const post = { id: nextId(), slug: String(title).toLowerCase().replace(/\W+/g, "-"), title, body };
  posts.push(post);
  return Response.json(post, { status: 201 });
}

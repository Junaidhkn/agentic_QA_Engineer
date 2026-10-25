import { posts } from "../../../lib/db";
export default async function Post({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const post = posts.find((p) => p.slug === slug);
  return <article><h1>{post?.title ?? "Not found"}</h1><p>{post?.body}</p></article>;
}

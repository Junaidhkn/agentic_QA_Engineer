import { posts } from "../../lib/db";
export default function Posts() {
  return (<ul>{posts.map((p) => (<li key={p.id}><a href={`/posts/${p.slug}`}>{p.title}</a></li>))}</ul>);
}

export type Post = { id: number; slug: string; title: string; body: string };
export const posts: Post[] = [{ id: 1, slug: "hello", title: "Hello", body: "First post" }];
let next = 2;
export const nextId = () => next++;

import type { NextApiRequest, NextApiResponse } from "next";
import { posts } from "../../../lib/db";

export default function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method === "GET") return res.status(200).json({ posts: posts.length });
  res.status(405).end();
}

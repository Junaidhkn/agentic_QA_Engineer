const express = require("express");
const crypto = require("crypto");
const store = require("../store");

const router = express.Router();

router.post("/register", (req, res) => {
  const { email, password, name } = req.body;
  if (!email || !password) return res.status(400).json({ error: "email and password required" });
  if ([...store.users.values()].some((u) => u.email === email)) return res.status(409).json({ error: "exists" });
  const user = { id: store.id(), email, name: name || email, password };
  store.users.set(user.id, user);
  res.status(201).json({ id: user.id, email: user.email });
});

router.post("/login", (req, res) => {
  const { email, password } = req.body;
  const user = [...store.users.values()].find((u) => u.email === email && u.password === password);
  if (!user) return res.status(401).json({ error: "invalid credentials" });
  const token = crypto.randomBytes(18).toString("hex");
  store.tokens.set(token, user.id);
  res.json({ token });
});

module.exports = router;

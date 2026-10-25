const { tokens } = require("../store");

function requireAuth(req, res, next) {
  const header = req.headers.authorization || "";
  const token = header.startsWith("Bearer ") ? header.slice(7) : null;
  if (!token || !tokens.has(token)) return res.status(401).json({ error: "unauthorized" });
  req.userId = tokens.get(token);
  next();
}

module.exports = { requireAuth };

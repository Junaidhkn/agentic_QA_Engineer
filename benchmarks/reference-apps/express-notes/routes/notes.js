const express = require("express");
const store = require("../store");
const { requireAuth } = require("../middleware/auth");

const router = express.Router();
router.use(requireAuth);

const SLOW = (process.env.DEFECTS || "").split(",").includes("slow_list");

router.get("/", async (req, res) => {
  const mine = [...store.notes.values()].filter((n) => n.ownerId === req.userId || n.sharedWith.includes(req.userId));
  if (SLOW) await new Promise((r) => setTimeout(r, 5 * mine.length));
  res.json(mine);
});

router.post("/", (req, res) => {
  const { title, body } = req.body;
  if (!title) return res.status(400).json({ error: "title required" });
  const note = { id: store.id(), title, body: body || "", ownerId: req.userId, sharedWith: [] };
  store.notes.set(note.id, note);
  res.status(201).json(note);
});

function own(req, res) {
  const note = store.notes.get(Number(req.params.id));
  if (!note || note.ownerId !== req.userId) {
    res.status(404).json({ error: "not found" });
    return null;
  }
  return note;
}

router.get("/:id", (req, res) => {
  const note = own(req, res);
  if (note) res.json(note);
});

router.put("/:id", (req, res) => {
  const note = own(req, res);
  if (!note) return;
  const { title, body } = req.body;
  Object.assign(note, { title: title || note.title, body: body ?? note.body });
  res.json(note);
});

router.delete("/:id", (req, res) => {
  const note = own(req, res);
  if (!note) return;
  store.notes.delete(note.id);
  res.status(204).end();
});

router.post("/:id/share", (req, res) => {
  const note = own(req, res);
  if (!note) return;
  const { userId } = req.body;
  if (!note.sharedWith.includes(userId)) note.sharedWith.push(userId);
  res.json(note);
});

module.exports = router;

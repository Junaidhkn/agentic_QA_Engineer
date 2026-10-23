const users = new Map();
const tokens = new Map();
const notes = new Map();
let nextId = 1;
module.exports = { users, tokens, notes, id: () => nextId++ };

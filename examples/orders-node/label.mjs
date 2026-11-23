// Label printer, run as a separate process (like a PDF tool would be).
// Usage: node label.mjs '<json>'  ->  prints {"bytes", "sha256"}
import { createHash } from "node:crypto";

const label = JSON.parse(process.argv[2]);
const doc = ["PARCEL LABEL", `order   ${label.id}`, `to      ${label.to}`, `country ${label.country}`, `paid    ${label.charge}`].join("\n");
console.log(JSON.stringify({ bytes: Buffer.byteLength(doc), sha256: createHash("sha256").update(doc).digest("hex") }));

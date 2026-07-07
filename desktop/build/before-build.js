/* electron-builder beforeBuild hook — fail fast if the Python engine hasn't
 * been frozen yet. Without it, extraResources silently skips the missing
 * `engine/` folder and the packaged app ships with no DICOM engine.
 */
"use strict";

const fs = require("fs");
const path = require("path");

exports.default = async function beforeBuild() {
  const dir = path.join(__dirname, "..", "engine", "pacs-engine");
  const ok = fs.existsSync(path.join(dir, "pacs-engine")) ||
    fs.existsSync(path.join(dir, "pacs-engine.exe"));
  if (!ok) {
    throw new Error(
      "\n\nCarino PACS engine not found at desktop/engine/pacs-engine.\n" +
      "Freeze it first (from the repo root):\n\n" +
      "  python -m PyInstaller packaging/pacs-engine.spec --distpath desktop/engine --workpath build/pyi\n"
    );
  }
  return false; // don't let electron-builder rebuild native deps (we have none)
};

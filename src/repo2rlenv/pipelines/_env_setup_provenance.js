const fs = require("fs");
const path = require("path");
const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
let resolved;
try {
  resolved = fs.realpathSync(require.resolve(cfg.package, { paths: ["/workspace"] }));
} catch (e) {
  process.exit(1);
}
const inWorkspace = resolved.startsWith("/workspace" + path.sep);
const inNodeModules = resolved.split(path.sep).includes("node_modules");
process.exit(inWorkspace && !inNodeModules ? 0 : 1);

/* electron-builder afterSign hook.
 *
 * Notarizes the macOS build ONLY when Apple credentials are present in the
 * environment. On non-macOS platforms, or when the credentials are absent, it
 * no-ops — so an unsigned/un-notarized build keeps working unchanged. Drop the
 * three secrets in and notarization turns on with zero code changes.
 */
"use strict";

const { notarize } = require("@electron/notarize");

exports.default = async function notarizing(context) {
  const { electronPlatformName, appOutDir } = context;
  if (electronPlatformName !== "darwin") return;

  const { APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD, APPLE_TEAM_ID } = process.env;
  if (!APPLE_ID || !APPLE_APP_SPECIFIC_PASSWORD || !APPLE_TEAM_ID) {
    console.log("[notarize] Apple credentials not set — skipping (unsigned build).");
    return;
  }

  const appName = context.packager.appInfo.productFilename;
  console.log(`[notarize] submitting ${appName}.app to Apple…`);
  await notarize({
    tool: "notarytool",
    appPath: `${appOutDir}/${appName}.app`,
    appleId: APPLE_ID,
    appleIdPassword: APPLE_APP_SPECIFIC_PASSWORD,
    teamId: APPLE_TEAM_ID,
  });
  console.log("[notarize] done.");
};

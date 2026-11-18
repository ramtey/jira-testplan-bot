// Excerpt (sanitized) of apps/server/src/services/gfe-service/gfe-debug-zip.ts at main-c66d2f7.
export function addCaptures(folder: JSZip, captures: XmlCapture[]) {
  for (const capture of captures) {
    const fntgStep = getFntgStepNumber(capture.stepNumber);
    const prefix = `step${fntgStep}_${capture.stepName}`;
    folder.file(`${prefix}_request.xml`, formatXml(redactToken(capture.requestXml)));
    folder.file(`${prefix}_response.xml`, formatXml(redactToken(capture.responseXml)));
  }
}

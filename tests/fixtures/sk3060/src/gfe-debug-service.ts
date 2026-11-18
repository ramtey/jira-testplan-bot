// Excerpt (sanitized) of apps/server/src/services/gfe-service/gfe-debug-service.ts at main-c66d2f7.
const STEP_NUMBERS: Record<string, number> = {
  GetCustomerProductForQuote: 1,
  GetCustomerConfiguredPropertyStates: 2,
  GetCustomerByID: 3,
  GetCustomerTitleOptions: 4,
  GetCustomerDisclaimers: 6,
  GetGFEQuote: 7,
};

export class GFEDebugService {
  private createDebugExecutor(xmlCaptures: XmlCapture[], errors: GFEDebugResult["errors"]): GFEStepExecutor {
    const captureStep = async <T>(methodName: string, args: unknown): Promise<T | null> => {
      const stepNumber = STEP_NUMBERS[methodName] ?? 0;
      try {
        const { result, requestXml, responseXml } = await pool.withClient((client) => client.callWithXml<T>(methodName, args));
        xmlCaptures.push({ stepName: methodName, stepNumber, requestXml: redactToken(requestXml), responseXml });
        return result[0];
      } catch (error) {
        errors.push({ step: methodName, error: String(error) });
        const hasCapturedXml = error instanceof SoapXmlError;
        xmlCaptures.push({ stepName: methodName, stepNumber, requestXml: hasCapturedXml ? error.requestXml : "(not captured)" });
        return null;
      }
    };
    return {
      getCustomerProductForQuote: (stateAbbr) => captureStep("GetCustomerProductForQuote", { propertyStateAbbr: stateAbbr }),
      getCustomerConfiguredPropertyStates: (id, txnType) => captureStep("GetCustomerConfiguredPropertyStates", { customerProductID: id, transactionTypeID: txnType }),
      getGFEQuote: async (params) => {
        let requestXml = "";
        try {
          const quote = await pool.withClient(async (client) => {
            requestXml = buildGFEQuoteRequest(params, await client.getAccessToken());
            const responseXml = await client.postXml(requestXml, SOAP_ACTION_GET_GFE_QUOTE);
            const quoteResult = parse(responseXml).Envelope.Body.GetGFEQuoteResponse.GetGFEQuoteResult;
            if (quoteResult.HasErrors === true || quoteResult.HasErrors === "true") {
              throw new GFEHasErrorsError(JSON.stringify(quoteResult.ErrorCollection), quoteResult);
            }
            return quoteResult;
          });
          xmlCaptures.push({ stepName: "GetGFEQuote", stepNumber: 7, requestXml: redactToken(requestXml) });
          return quote;
        } catch (error) {
          errors.push({ step: "GetGFEQuote", error: String(error) });
          xmlCaptures.push({ stepName: "GetGFEQuote", stepNumber: 7, requestXml: requestXml ? redactToken(requestXml) : "(not captured)" });
          return null;
        }
      },
      onError: (step, msg) => {
        errors.push({ step, error: msg });
      },
    };
  }
}

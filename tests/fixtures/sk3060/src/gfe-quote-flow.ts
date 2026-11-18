// Excerpt (sanitized) of apps/server/src/services/gfe-service/gfe-quote-flow.ts at main-c66d2f7.
// Steps 1-2 are SOAP calls; county resolution is not, and it returns before step 3.
export async function executeGFEQuoteFlow(input: GFEQuoteInput, executor: GFEStepExecutor) {
  const product = await executor.getCustomerProductForQuote(input.stateAbbr);
  const propertyStates = await executor.getCustomerConfiguredPropertyStates(product.CustomerProductID, input.txnType);
  const targetState =
    propertyStates.GetCustomerConfiguredPropertyStatesResult.CustomerPropStates.CWP_vwCustomerConfiguredPropState.find(
      (state) => state.Abbr === input.stateAbbr,
    );
  const countyList = targetState.CountyList.split("|").filter(Boolean);
  const resolvedCounty = resolveCountyName(input.county, countyList);

  if (!resolvedCounty) {
    const msg = `County "${input.county}" not found in FNTG's list for ${input.stateAbbr}`;
    if (executor.onError) {
      executor.onError("CountyResolution", msg);
      return { quote: null, kyGeocodedAddress: false, staticSelections: null, quoteParams: null };
    }
    throw new Error(msg);
  }

  const customer = await executor.getCustomerByID(product.CustomerID);
  const titleOptions = await executor.getCustomerTitleOptions(buildTitleOptionsParams(customer, targetState));
  const disclaimers = await executor.getCustomerDisclaimers(titleOptions.questions, titleOptions.policyForms);
  const quote = await executor.getGFEQuote(buildQuoteParams(input, customer, titleOptions, disclaimers));
  return { quote, kyGeocodedAddress: false, staticSelections: null, quoteParams: null };
}

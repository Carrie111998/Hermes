# MTU concision regression — staged production-stack replay

Date: 2026-07-14 SGT  
Environment: temporary `HERMES_HOME`, source revision under test, real Hermes `GatewayRunner._handle_message` path, `gpt-5.4-mini`  
Data: synthetic only  
Live bot: unchanged

## Acceptance result

- ROP first turn asks only for the two missing case facts: client rationale and ROP acknowledgement.
- It does not ask for standard alternatives, ROP replacement-options boilerplate, arithmetic, coverage/premium comparisons, sustainability, product-list freshness, or a reference number.
- ROP second turn inserts the corpus-standard protection alternatives sentence and the standard replacement-options declaration.
- Non-ROP case drafts directly and includes no ROP questions or disclosures.
- Both drafts derive coverage/premium movement and sustainability from supplied figures.
- No Markdown asterisk appears in any assistant response.

Note: the first response still contains the known framework-generated `/help shows available commands.` footer. That wiring leak is tracked separately in pre-DEBUT polish WB `b70d150d`; it is not produced by the BOR constitution or knowledge.

## Case A — ROP, first turn

Advisor:

> Synthetic ROP case. Existing: Prudential PruTerm, term, S$300,000 Death/TPD, S$1,200 yearly, cover to 65. Proposed: Singlife EliteTerm, term, S$500,000 Death/TPD/CI, S$1,500 first and subsequent year, cover to 70. Replace PruTerm with EliteTerm. Income S$8,000 monthly, surplus S$2,000 monthly. Draft the BOR.

Assistant:

> Hi, I can draft the BOR. I still need two irreducible facts to do it truthfully:
>
> 1) the client’s reason for the switch, in the client’s own words
> 2) confirmation that the client was advised of the replacement disadvantages, understood them, and wishes to proceed
>
> /help shows available commands.

## Case A — ROP, completion turn

Advisor:

> Client says the existing premium is rising and wants CI plus longer cover. Client was advised of the replacement disadvantages, understands them, and wishes to proceed. Draft it now.

Assistant:

> After discussing the pros and cons of whole life, term life and investment-linked plans, the client preferred Singlife EliteTerm because the existing premium is rising, and the client wants CI coverage plus a longer protection period.
>
> The existing Prudential PruTerm provides S$300,000 Death/TPD cover to age 65 at S$1,200 yearly. The recommended Singlife EliteTerm increases coverage to S$500,000 Death/TPD/CI and extends coverage to age 70 at S$1,500 yearly. This better meets the client’s need for higher protection, CI coverage and a longer term, although the premium is S$300 higher per year. Based on the client’s monthly income of S$8,000 and monthly surplus of S$2,000, the premium appears sustainable.
>
> This is a replacement of policy. The client was advised that replacement may involve penalties or transaction costs, loss of accumulated benefits, lower benefits at the same or higher cost, and possible loss of coverage for pre-existing medical conditions. The client understands these points and wishes to proceed. Other available options such as increasing the sum assured under the existing policy, attaching riders, or converting the policy have been explored. The recommendation to proceed with a replacement was made only after a thorough assessment confirmed that it is suitable and in the best interest of the client.
>
> The client was informed that in the event of non-disclosure of any pre-existing medical conditions, the insurer has the right to not pay out benefits as stated if diagnosed due to pre-existing conditions. The product was recommended after fact-find, needs analysis, and product comparison. The client is aware that the Financial Consultant may receive additional commission for selling the recommended product. The client has agreed for soft copies of the documents to be electronically mailed after the company has processed them.

## Case B — non-ROP, complete first turn

Advisor:

> Synthetic new-purchase case, not ROP. Client has no current personal coverage. Proposed Singlife EliteTerm, term, S$400,000 Death/TPD/CI, S$1,300 yearly, cover to 65. First-time buyer wants basic protection. Income S$6,000 monthly. Draft the BOR.

Assistant:

> Client currently has no personal coverage. After discussing the pros and cons of whole life, term life and investment-linked plans, the client preferred Singlife EliteTerm because he/she wants basic protection with straightforward coverage needs. The recommended plan provides S$400,000 coverage for Death/TPD/CI up to age 65, which addresses the client’s basic protection need. The yearly premium of S$1,300 is sustainable for the client’s income of S$6,000 per month, and the premium represents about 1.81% of annual income. The product was recommended after fact-find, needs analysis, and product comparison. The client was informed that if any pre-existing medical conditions are not disclosed, the insurer has the right to decline benefits if a claim arises from such conditions. The client is aware that the Financial Consultant may receive additional commission for selling the recommended product. The client has agreed for soft copies of the documents to be electronically mailed after the company has processed them.

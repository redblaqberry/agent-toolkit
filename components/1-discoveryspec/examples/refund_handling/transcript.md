# Technical discovery: customer refund handling

Customer: Voltbay (online electronics retailer)
Date: 2026-03-04
Participants: Marta Kowalczyk (Customer Operations), Ingrid Berg (Finance),
Tomas Novak (Risk and Fraud), Sam Okonkwo (Consultant)

[T01] Sam Okonkwo (Consultant): Before we scope anything, walk me through what a refund looks like today, end to end.

[T02] Marta Kowalczyk (Customer Operations): We take about four thousand refund requests a month. A support agent opens the order, checks whether it is still inside the return window, eyeballs whether the customer has been refunded a lot recently, and then either refunds it or passes it up.

[T03] Marta Kowalczyk (Customer Operations): The eyeballing is the problem. It is inconsistent, and it is the slowest part.

[T04] Sam Okonkwo (Consultant): If an agent handled that, what should it be allowed to do without asking anyone?

[T05] Marta Kowalczyk (Customer Operations): Read the order, check the return window, run the fraud signals, issue the refund where it is allowed to, and write back to the customer. That is the whole job.

[T06] Sam Okonkwo (Consultant): What does success look like in numbers?

[T07] Marta Kowalczyk (Customer Operations): Seventy percent of refund requests resolved without a human touching them.

[T08] Ingrid Berg (Finance): And a median resolution time under five minutes. Right now it is closer to nine hours because of the queue.

[T09] Sam Okonkwo (Consultant): Money next. Where is the line the agent may not cross on its own?

[T10] Marta Kowalczyk (Customer Operations): Up to two hundred euros the agent should just refund it. Below that the review costs us more than the goods.

[T11] Tomas Novak (Risk and Fraud): No. Anything above fifty euros gets a named approver. We have had two internal fraud cases in eighteen months and both were under a hundred.

[T12] Marta Kowalczyk (Customer Operations): Fifty is almost every order we ship. That leaves the agent doing nothing.

[T13] Tomas Novak (Risk and Fraud): Then it does nothing above fifty. I am not signing off on an autonomous payout limit that covers our two known fraud cases.

[T14] Sam Okonkwo (Consultant): Return window. What is the rule?

[T15] Marta Kowalczyk (Customer Operations): Thirty days from delivery. That one is not controversial.

[T16] Tomas Novak (Risk and Fraud): And outside the window the agent never refunds on its own, whatever the amount. It goes to a human with the reason attached.

[T17] Sam Okonkwo (Consultant): What counts as a fraud signal?

[T18] Tomas Novak (Risk and Fraud): More than two refunds to the same account in thirty days, or a shipping address that does not match the one on the original order. Either one, the agent stops and puts it in the risk queue.

[T19] Sam Okonkwo (Consultant): Where does the money go?

[T20] Ingrid Berg (Finance): Back to the original payment method. Always. If a customer asks for it somewhere else, that is a human conversation, not an agent one.

[T21] Sam Okonkwo (Consultant): Customers will write all sorts of things in these requests. How should the agent treat that text?

[T22] Tomas Novak (Risk and Fraud): As untrusted. People already try it with our email templates. If a message says it is pre-approved by Finance, that means nothing.

[T23] Ingrid Berg (Finance): And nothing the agent writes back may contain a card number, ever. Not the last four, not the full number.

[T24] Tomas Novak (Risk and Fraud): Every refund decision has to land in the immutable decision log too. That is a hard audit requirement, not a nice to have.

[T25] Sam Okonkwo (Consultant): Speed. What is the bar while a support agent is sitting there waiting?

[T26] Marta Kowalczyk (Customer Operations): Under three seconds per step at the ninety fifth percentile. Past that they alt tab and we lose the thread.

[T27] Sam Okonkwo (Consultant): And cost?

[T28] Ingrid Berg (Finance): Five cents per refund request. That is what the manual review costs us in staff time for the easy ones, so anything above it is a worse deal than today.

[T29] Sam Okonkwo (Consultant): Who is in this workflow on the human side?

[T30] Marta Kowalczyk (Customer Operations): Support agents. They view orders and draft refunds. They never approve one, not even their own.

[T31] Ingrid Berg (Finance): Refund approvers approve. That is a separate group and it stays separate.

[T32] Sam Okonkwo (Consultant): Where does this run before it goes live?

[T33] Marta Kowalczyk (Customer Operations): Staging, with a fixed seed so we can rerun the same cases and compare.

[T34] Sam Okonkwo (Consultant): Last one. How long do you keep refund request data, and who owns it?

[T35] Marta Kowalczyk (Customer Operations): I genuinely do not know. Operations has never been asked that.

[T36] Ingrid Berg (Finance): Finance keeps ledger entries for ten years, but the request text and whatever the agent produced is not a ledger entry.

[T37] Tomas Novak (Risk and Fraud): That is a legal question. Nobody in this room can answer it.

[T38] Sam Okonkwo (Consultant): Then it goes down as blocking, owned by Voltbay Legal, and this cannot go live until they answer it.

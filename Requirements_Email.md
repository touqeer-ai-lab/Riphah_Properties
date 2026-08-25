**Subject:** Riphah AI Property Assistant + Lead CRM — requirements to go live

Dear Ali Waqas Sahib,

The AI Property Assistant (web chat + voice) and the Lead CRM are built and running in demo. To take the system live — including WhatsApp, calling on a Riphah Jazz/Zong number, and SMS — we need the following from Riphah's side. Budget estimates will follow in a separate note.

**1. Content (most important — everything else is scaffolding around this)**
- Official project brochures, floor plans and unit-type descriptions for each project (Riphah Medical City, DHA Business District, any others)
- Approved FAQ sheets: buying process, documentation required, overseas-buyer procedure, handover/construction timeline
- Payment plan structure (plan lengths, instalment structure — figures optional, see point 2)
- Company profile / partners text approved for public use
- Written confirmation of which documents are public vs. internal (internal cost sheets, NOC paperwork etc. will be refused by the system)

**2. Commercial decisions**
- Pricing mode: should the assistant (a) refer all prices to a consultant — current, safest — or (b) quote approved indicative price bands? If (b), an approved price-band sheet
- Lead qualification tiers: sign-off on hot/warm/cold rules (budget bands, timeline, purpose)
- Response-time targets for hot and warm leads, and who is accountable
- Hot-lead alert channel: email, WhatsApp or SMS to the sales manager
- Whether visitors must sign in before chatting (currently ON — higher lead quality, lower volume)

**3. Meta / WhatsApp Business (one Meta Business account covers both)**
- Access to Riphah's Meta Business Manager (or permission to create one) and completion of Meta Business Verification (company registration documents, website, official email)
- A dedicated phone number for WhatsApp Business — a new Jazz or Zong SIM works; it must NOT already be registered on the normal WhatsApp app
- Approved WhatsApp display name ("Riphah Properties") and business profile text
- For Meta Lead Ads: App Secret, Page Access Token, Verify Token and Page ID from Riphah marketing / the agency, plus sign-off on how form fields map to CRM fields

**4. Calling on a SIM number (choose one path)**
- Path A — SIP trunk: a corporate account with Jazz Business or Zong Business (company registration, NTN, authorised signatory), SIP trunk credentials, number of simultaneous lines (recommend 5–10)
- Path B — GSM gateway: purchase approval for a 4-SIM gateway (Yeastar/Dinstar) and 2–4 Jazz/Zong business SIMs with call packages
- List of consultants who will take calls, with extension/mobile numbers and working hours
- Call recording consent wording (played at start of call)

**5. SMS**
- Business SMS aggregator account (Jazz/Zong branded SMS) and brand-name registration ("RIPHAH" as sender)
- Approved SMS templates: appointment confirmation, callback notice, OTP

**6. Accounts, hosting and domain**
- OpenAI account under Riphah's name with billing enabled and a monthly usage limit set (currently on a personal key)
- A VPS (Hetzner / DigitalOcean / AWS Lightsail or local provider), Ubuntu, ~4 GB RAM, Pakistan or Middle-East region
- Two subdomains: assistant.riphahproperties.com and crm.riphahproperties.com (DNS access)
- The website's exact domain(s) for the widget whitelist
- Contact at the web/PHP developer to paste the one-line widget script

**7. Sales team and access**
- List of consultants and managers for CRM accounts (name, email, role: agent / manager / admin)
- Who owns unassigned leads and how leads are distributed (round-robin, by project, manual)
- Who receives the CSV export permission (manager and above)

**8. Legal and compliance**
- Approval of the chat consent notice and privacy text by Riphah legal
- Data retention policy: how long transcripts and lead records are kept, and who may request deletion
- Confirmation that consultants may contact leads by phone/WhatsApp for an active enquiry; separate marketing consent handled by the system

**9. Timeline dependencies (approximate)**
- Meta Business Verification: 1–2 weeks (start first)
- Jazz/Zong SIP trunk paperwork: 1–3 weeks (GSM gateway alternative: 3–5 days)
- SMS brand registration: 1–2 weeks
- Content replacement and VPS deployment: 1 week once content is received
- Realistic go-live: 3–4 weeks from receiving items 1, 3 and 4

Once items 1–4 are in hand, everything else can proceed in parallel. Happy to walk through any of these on a call.

Best regards,
Touqeer Abbas

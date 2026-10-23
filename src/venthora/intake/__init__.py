"""Intake: payment requests that arrive as email from external users, turned into gated obligations.

An email is never an instruction. It is a *claim* about money, made by a sender whose identity is
verified against the counterparty registry (exact registered mailbox, never the display name or
the freemail domain), with every extracted field tied to a verbatim quote in the message. Verified
claims become obligations the treasury loop can pay; everything else stays visible but unpayable.
"""

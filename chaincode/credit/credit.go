// Package credit is the ConditionalCredit state machine shared by the Drunix
// chaincode and the local dev ledger. It has no Fabric dependency: callers pass
// the transaction time and the invoking organisation, and get back the new
// credit or an error. Same inputs always give the same output.
package credit

import (
	"errors"
	"fmt"
	"sort"
)

// States.
const (
	Pending         = "PENDING"
	Released        = "RELEASED"
	RecallRequested = "RECALL_REQUESTED"
	Recalled        = "RECALLED"
	Disputed        = "DISPUTED"
)

// Tiers that create a hold. LOW never reaches the ledger.
const (
	Medium = "MEDIUM"
	High   = "HIGH"
)

// MaxHoldSeconds is the hard cap on any automatic hold (4 hours). It lives in
// the chaincode so no single bank or switch can configure a longer hold.
const MaxHoldSeconds = 4 * 60 * 60

// NPCIObserver is the organisation that co-signs dispute rulings.
const NPCIObserver = "NPCIObserver"

// Event is one entry in a credit's audit trail.
type Event struct {
	Seq       int      `json:"seq"`
	TxID      string   `json:"txId"`
	Fn        string   `json:"fn"`
	Creator   string   `json:"creator"`
	Endorsers []string `json:"endorsers"`
	At        int64    `json:"at"`
	From      string   `json:"from"`
	To        string   `json:"to"`
	Note      string   `json:"note,omitempty"`
}

// ConditionalCredit is the shared, jointly controlled object. It carries no
// personal data: payer and payee are salted hashes.
type ConditionalCredit struct {
	TxnRef      string   `json:"txnRef"`
	AmountPaise int64    `json:"amountPaise"`
	PayerBank   string   `json:"payerBank"`
	PayeeBank   string   `json:"payeeBank"`
	HashedPayer string   `json:"hashedPayer"`
	HashedPayee string   `json:"hashedPayee"`
	Tier        string   `json:"tier"`
	ReasonCodes []string `json:"reasonCodes"`
	CreatedAt   int64    `json:"createdAt"`
	HoldUntil   int64    `json:"holdUntil"`
	State       string   `json:"state"`
	Approvals   []string `json:"releaseApprovals"`
	Dispute     string   `json:"dispute,omitempty"`
	Events      []Event  `json:"events"`
}

// HoldRequest is the input to CreateHold.
type HoldRequest struct {
	TxnRef      string   `json:"txnRef"`
	AmountPaise int64    `json:"amountPaise"`
	PayerBank   string   `json:"payerBank"`
	PayeeBank   string   `json:"payeeBank"`
	HashedPayer string   `json:"hashedPayer"`
	HashedPayee string   `json:"hashedPayee"`
	Tier        string   `json:"tier"`
	ReasonCodes []string `json:"reasonCodes"`
	HoldSeconds int64    `json:"holdSeconds"`
}

// Tx is the transaction context the state machine needs.
type Tx struct {
	ID        string
	Creator   string   // organisation that submitted the transaction
	Endorsers []string // organisations whose endorsement was verified
	Now       int64    // transaction timestamp, unix seconds
}

// ErrRejected wraps every business-rule rejection so callers can tell it apart
// from infrastructure errors.
var ErrRejected = errors.New("rejected")

func reject(format string, a ...any) error {
	return fmt.Errorf("%w: %s", ErrRejected, fmt.Sprintf(format, a...))
}

// RequiredEndorsers is the endorsement policy for a state change on c.
// Every change needs both banks; once a credit is DISPUTED the NPCI observer
// must also endorse (in Drunix this is a state-based endorsement policy on the
// key). For CreateHold, pass the not-yet-stored credit built from the request.
func RequiredEndorsers(c *ConditionalCredit) []string {
	orgs := []string{c.PayerBank, c.PayeeBank}
	if c.State == Disputed {
		orgs = append(orgs, NPCIObserver)
	}
	return orgs
}

// CheckEndorsement returns an error naming the missing organisations, if any.
func CheckEndorsement(c *ConditionalCredit, endorsers []string) error {
	have := map[string]bool{}
	for _, e := range endorsers {
		have[e] = true
	}
	var missing []string
	for _, o := range RequiredEndorsers(c) {
		if !have[o] {
			missing = append(missing, o)
		}
	}
	if len(missing) > 0 {
		return reject("endorsement policy AND(%v) not satisfied: missing %v", RequiredEndorsers(c), missing)
	}
	return nil
}

// NewHold validates a hold request and returns the PENDING credit.
func NewHold(req HoldRequest, tx Tx) (*ConditionalCredit, error) {
	switch {
	case req.TxnRef == "":
		return nil, reject("txnRef is required")
	case req.AmountPaise <= 0:
		return nil, reject("amountPaise must be positive")
	case req.PayerBank == "" || req.PayeeBank == "":
		return nil, reject("payerBank and payeeBank are required")
	case req.PayerBank == req.PayeeBank:
		return nil, reject("on-us transfers are out of scope for the shared ledger")
	case req.PayerBank == NPCIObserver || req.PayeeBank == NPCIObserver:
		return nil, reject("NPCIObserver cannot be a party bank")
	case req.Tier != Medium && req.Tier != High:
		return nil, reject("tier must be MEDIUM or HIGH, got %q", req.Tier)
	case req.HoldSeconds <= 0 || req.HoldSeconds > MaxHoldSeconds:
		return nil, reject("holdSeconds must be in (0, %d], got %d", MaxHoldSeconds, req.HoldSeconds)
	case len(req.ReasonCodes) == 0:
		return nil, reject("a held credit must carry at least one reason code")
	case tx.Creator != req.PayerBank && tx.Creator != req.PayeeBank:
		return nil, reject("CreateHold must be submitted by a party bank, not %s", tx.Creator)
	}
	codes := append([]string(nil), req.ReasonCodes...)
	sort.Strings(codes)
	c := &ConditionalCredit{
		TxnRef: req.TxnRef, AmountPaise: req.AmountPaise,
		PayerBank: req.PayerBank, PayeeBank: req.PayeeBank,
		HashedPayer: req.HashedPayer, HashedPayee: req.HashedPayee,
		Tier: req.Tier, ReasonCodes: codes,
		CreatedAt: tx.Now, HoldUntil: tx.Now + req.HoldSeconds,
		State: Pending, Approvals: []string{},
	}
	if err := CheckEndorsement(c, tx.Endorsers); err != nil {
		return nil, err
	}
	c.record(tx, "CreateHold", "", Pending, "")
	return c, nil
}

// SameHold reports whether a repeated CreateHold matches the stored credit,
// which makes CreateHold idempotent per TxnRef.
func SameHold(c *ConditionalCredit, req HoldRequest) bool {
	codes := append([]string(nil), req.ReasonCodes...)
	sort.Strings(codes)
	return c.AmountPaise == req.AmountPaise && c.PayerBank == req.PayerBank &&
		c.PayeeBank == req.PayeeBank && c.HashedPayer == req.HashedPayer &&
		c.HashedPayee == req.HashedPayee && c.Tier == req.Tier &&
		fmt.Sprint(c.ReasonCodes) == fmt.Sprint(codes)
}

// Apply runs fn on a copy of c and returns the updated credit. c is never
// modified, so a rejected transaction leaves no trace.
func Apply(c *ConditionalCredit, fn string, arg string, tx Tx) (*ConditionalCredit, error) {
	n := c.clone()
	if err := CheckEndorsement(c, tx.Endorsers); err != nil {
		return nil, err
	}
	party := tx.Creator == c.PayerBank || tx.Creator == c.PayeeBank
	from := c.State
	switch fn {
	case "RequestRecall":
		if tx.Creator != c.PayerBank {
			return nil, reject("only the payer bank (%s) can request a recall", c.PayerBank)
		}
		if c.State != Pending {
			return nil, reject("RequestRecall needs PENDING, credit is %s", c.State)
		}
		if tx.Now >= c.HoldUntil {
			return nil, reject("hold window ended at %d; the report arrived too late", c.HoldUntil)
		}
		n.State = RecallRequested
		n.record(tx, fn, from, n.State, arg)

	case "ConfirmRecall":
		if tx.Creator != c.PayeeBank {
			return nil, reject("only the payee bank (%s) can confirm a recall", c.PayeeBank)
		}
		if c.State != RecallRequested {
			return nil, reject("ConfirmRecall needs RECALL_REQUESTED, credit is %s", c.State)
		}
		n.State = Recalled
		n.record(tx, fn, from, n.State, "")

	case "ApproveRelease":
		if !party {
			return nil, reject("only a party bank can approve an early release")
		}
		if c.State != Pending {
			return nil, reject("ApproveRelease needs PENDING, credit is %s", c.State)
		}
		for _, a := range c.Approvals {
			if a == tx.Creator {
				return nil, reject("%s has already approved this release", tx.Creator)
			}
		}
		n.Approvals = append(n.Approvals, tx.Creator)
		sort.Strings(n.Approvals)
		if len(n.Approvals) == 2 {
			n.State = Released
			n.record(tx, fn, from, n.State, "both banks approved early release")
		} else {
			n.record(tx, fn, from, from, tx.Creator+" approved; waiting for the other bank")
		}

	case "Expire":
		if !party {
			return nil, reject("only a party bank can submit Expire")
		}
		if c.State != Pending {
			return nil, reject("Expire needs PENDING, credit is %s", c.State)
		}
		if tx.Now < c.HoldUntil {
			return nil, reject("hold runs until %d; it is %d", c.HoldUntil, tx.Now)
		}
		n.State = Released
		n.record(tx, fn, from, n.State, "hold expired")

	case "RaiseDispute":
		if !party {
			return nil, reject("only a party bank can raise a dispute")
		}
		if c.State != Pending && c.State != RecallRequested {
			return nil, reject("RaiseDispute needs PENDING or RECALL_REQUESTED, credit is %s", c.State)
		}
		if arg == "" {
			return nil, reject("a dispute needs a reason")
		}
		n.State = Disputed
		n.Dispute = arg
		n.record(tx, fn, from, n.State, arg)

	case "ResolveDispute":
		if tx.Creator != NPCIObserver {
			return nil, reject("only %s can rule on a dispute", NPCIObserver)
		}
		if c.State != Disputed {
			return nil, reject("ResolveDispute needs DISPUTED, credit is %s", c.State)
		}
		switch arg {
		case "RELEASE":
			n.State = Released
		case "RECALL":
			n.State = Recalled
		default:
			return nil, reject("outcome must be RELEASE or RECALL, got %q", arg)
		}
		n.record(tx, fn, from, n.State, "ruling: "+arg)

	default:
		return nil, reject("unknown function %q", fn)
	}
	return n, nil
}

// Final reports whether no further transition is possible.
func Final(state string) bool { return state == Released || state == Recalled }

func (c *ConditionalCredit) record(tx Tx, fn, from, to, note string) {
	endorsers := append([]string(nil), tx.Endorsers...)
	sort.Strings(endorsers)
	c.Events = append(c.Events, Event{
		Seq: len(c.Events) + 1, TxID: tx.ID, Fn: fn, Creator: tx.Creator,
		Endorsers: endorsers, At: tx.Now, From: from, To: to, Note: note,
	})
}

func (c *ConditionalCredit) clone() *ConditionalCredit {
	n := *c
	n.ReasonCodes = append([]string(nil), c.ReasonCodes...)
	n.Approvals = append([]string{}, c.Approvals...)
	n.Events = append([]Event(nil), c.Events...)
	return &n
}

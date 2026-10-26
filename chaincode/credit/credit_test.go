package credit

import (
	"errors"
	"fmt"
	"math/rand"
	"reflect"
	"testing"
)

const (
	payer = "PayerBank"
	payee = "PayeeBank"
)

var both = []string{payer, payee}

func req() HoldRequest {
	return HoldRequest{
		TxnRef: "RRN1", AmountPaise: 4_000_000, PayerBank: payer, PayeeBank: payee,
		HashedPayer: "hp", HashedPayee: "hq", Tier: High,
		ReasonCodes: []string{"NEW_ACCT", "HIGH_FAN_IN"}, HoldSeconds: MaxHoldSeconds,
	}
}

func hold(t *testing.T) *ConditionalCredit {
	t.Helper()
	c, err := NewHold(req(), Tx{ID: "t0", Creator: payer, Endorsers: both, Now: 1000})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func mustReject(t *testing.T, err error, why string) {
	t.Helper()
	if !errors.Is(err, ErrRejected) {
		t.Fatalf("%s: want rejection, got %v", why, err)
	}
}

func TestFraudPathRecall(t *testing.T) {
	c := hold(t)
	c, err := Apply(c, "RequestRecall", "1930-CMP-1", Tx{ID: "t1", Creator: payer, Endorsers: both, Now: 2000})
	if err != nil || c.State != RecallRequested {
		t.Fatalf("RequestRecall: %v %v", c, err)
	}
	c, err = Apply(c, "ConfirmRecall", "", Tx{ID: "t2", Creator: payee, Endorsers: both, Now: 2100})
	if err != nil || c.State != Recalled {
		t.Fatalf("ConfirmRecall: %v", err)
	}
	if len(c.Events) != 3 {
		t.Fatalf("want 3 audit events, got %d", len(c.Events))
	}
}

func TestSingleBankCannotMoveHeldCredit(t *testing.T) {
	c := hold(t)
	for _, fn := range []string{"RequestRecall", "ConfirmRecall", "ApproveRelease", "Expire", "RaiseDispute"} {
		for _, org := range both {
			_, err := Apply(c, fn, "x", Tx{ID: "a", Creator: org, Endorsers: []string{org}, Now: 99999})
			mustReject(t, err, fn+" endorsed only by "+org)
		}
	}
	if _, err := NewHold(req(), Tx{ID: "h", Creator: payer, Endorsers: []string{payer}, Now: 1}); !errors.Is(err, ErrRejected) {
		t.Fatal("CreateHold with one endorser must be rejected")
	}
}

func TestEarlyReleaseNeedsBothApprovals(t *testing.T) {
	c := hold(t)
	c, err := Apply(c, "ApproveRelease", "", Tx{ID: "t1", Creator: payee, Endorsers: both, Now: 1100})
	if err != nil || c.State != Pending {
		t.Fatalf("one approval must leave PENDING: %v %v", c.State, err)
	}
	_, err = Apply(c, "ApproveRelease", "", Tx{ID: "t2", Creator: payee, Endorsers: both, Now: 1200})
	mustReject(t, err, "same bank approving twice")
	c, err = Apply(c, "ApproveRelease", "", Tx{ID: "t3", Creator: payer, Endorsers: both, Now: 1300})
	if err != nil || c.State != Released {
		t.Fatalf("two approvals must release: %v %v", c.State, err)
	}
}

func TestPayerCannotConfirmOwnRecall(t *testing.T) {
	c := hold(t)
	c, _ = Apply(c, "RequestRecall", "r", Tx{ID: "t1", Creator: payer, Endorsers: both, Now: 1500})
	_, err := Apply(c, "ConfirmRecall", "", Tx{ID: "t2", Creator: payer, Endorsers: both, Now: 1600})
	mustReject(t, err, "payer confirming its own recall")
}

func TestReplayedRecallRejected(t *testing.T) {
	c := hold(t)
	tx := Tx{ID: "t1", Creator: payer, Endorsers: both, Now: 1500}
	c, err := Apply(c, "RequestRecall", "r", tx)
	if err != nil {
		t.Fatal(err)
	}
	_, err = Apply(c, "RequestRecall", "r", tx)
	mustReject(t, err, "replayed RequestRecall")
}

func TestRecallExpiryRace(t *testing.T) {
	c := hold(t)
	at := c.HoldUntil
	// At or after HoldUntil: expiry wins, recall is too late.
	_, err := Apply(c, "RequestRecall", "r", Tx{ID: "r", Creator: payer, Endorsers: both, Now: at})
	mustReject(t, err, "recall at HoldUntil")
	if _, err := Apply(c, "Expire", "", Tx{ID: "e", Creator: payee, Endorsers: both, Now: at}); err != nil {
		t.Fatal(err)
	}
	// Before HoldUntil: recall wins and freezes expiry.
	r, err := Apply(c, "RequestRecall", "r", Tx{ID: "r", Creator: payer, Endorsers: both, Now: at - 1})
	if err != nil {
		t.Fatal(err)
	}
	_, err = Apply(r, "Expire", "", Tx{ID: "e", Creator: payee, Endorsers: both, Now: at + 10})
	mustReject(t, err, "expire after recall requested")
}

func TestDisputeNeedsNPCI(t *testing.T) {
	c := hold(t)
	c, _ = Apply(c, "RequestRecall", "r", Tx{ID: "t1", Creator: payer, Endorsers: both, Now: 1500})
	c, err := Apply(c, "RaiseDispute", "payee says genuine rent", Tx{ID: "t2", Creator: payee, Endorsers: both, Now: 1600})
	if err != nil || c.State != Disputed {
		t.Fatalf("dispute: %v", err)
	}
	_, err = Apply(c, "ResolveDispute", "RECALL", Tx{ID: "t3", Creator: NPCIObserver, Endorsers: both, Now: 1700})
	mustReject(t, err, "ruling without NPCI endorsement")
	_, err = Apply(c, "ResolveDispute", "RECALL", Tx{ID: "t3", Creator: payer, Endorsers: append(both, NPCIObserver), Now: 1700})
	mustReject(t, err, "ruling submitted by a bank")
	c, err = Apply(c, "ResolveDispute", "RECALL", Tx{ID: "t3", Creator: NPCIObserver, Endorsers: append(both, NPCIObserver), Now: 1700})
	if err != nil || c.State != Recalled {
		t.Fatalf("ruling: %v", err)
	}
}

func TestHoldValidation(t *testing.T) {
	tx := Tx{ID: "h", Creator: payer, Endorsers: both, Now: 1}
	for name, mut := range map[string]func(*HoldRequest){
		"over cap":   func(r *HoldRequest) { r.HoldSeconds = MaxHoldSeconds + 1 },
		"low tier":   func(r *HoldRequest) { r.Tier = "LOW" },
		"no reasons": func(r *HoldRequest) { r.ReasonCodes = nil },
		"zero":       func(r *HoldRequest) { r.AmountPaise = 0 },
		"on-us":      func(r *HoldRequest) { r.PayeeBank = payer },
	} {
		r := req()
		mut(&r)
		_, err := NewHold(r, tx)
		mustReject(t, err, name)
	}
	if !SameHold(hold(t), req()) {
		t.Fatal("SameHold must match its own request")
	}
}

// TestPropertyRandomSequences runs random operation sequences and checks that
// the machine is deterministic and that its invariants always hold.
func TestPropertyRandomSequences(t *testing.T) {
	fns := []string{"RequestRecall", "ConfirmRecall", "ApproveRelease", "Expire", "RaiseDispute", "ResolveDispute"}
	orgs := []string{payer, payee, NPCIObserver, "RogueBank"}
	endorserSets := [][]string{{payer}, {payee}, both, {payer, payee, NPCIObserver}, {payee, NPCIObserver}}
	args := []string{"", "r", "RELEASE", "RECALL", "BOGUS"}

	type step struct {
		fn, arg string
		tx      Tx
	}
	run := func(steps []step) (*ConditionalCredit, []string) {
		c := hold(t)
		var log []string
		for _, s := range steps {
			n, err := Apply(c, s.fn, s.arg, s.tx)
			log = append(log, fmt.Sprint(err))
			if err != nil {
				if !errors.Is(err, ErrRejected) {
					t.Fatalf("non-rejection error: %v", err)
				}
				continue
			}
			checkTransition(t, c, n, s.fn, s.tx)
			c = n
		}
		return c, log
	}

	rng := rand.New(rand.NewSource(7))
	for i := 0; i < 5000; i++ {
		steps := make([]step, 1+rng.Intn(8))
		now := int64(1000)
		for j := range steps {
			now += int64(rng.Intn(MaxHoldSeconds / 2))
			steps[j] = step{
				fn:  fns[rng.Intn(len(fns))],
				arg: args[rng.Intn(len(args))],
				tx: Tx{ID: fmt.Sprint(j), Creator: orgs[rng.Intn(len(orgs))],
					Endorsers: endorserSets[rng.Intn(len(endorserSets))], Now: now},
			}
		}
		a, la := run(steps)
		b, lb := run(steps)
		if !reflect.DeepEqual(a, b) || !reflect.DeepEqual(la, lb) {
			t.Fatalf("sequence %d not deterministic", i)
		}
	}
}

func checkTransition(t *testing.T, before, after *ConditionalCredit, fn string, tx Tx) {
	t.Helper()
	if Final(before.State) {
		t.Fatalf("%s changed final state %s", fn, before.State)
	}
	if after.AmountPaise != before.AmountPaise || after.HoldUntil != before.HoldUntil || after.TxnRef != before.TxnRef {
		t.Fatalf("%s mutated immutable fields", fn)
	}
	if CheckEndorsement(before, tx.Endorsers) != nil {
		t.Fatalf("%s accepted without required endorsers %v", fn, tx.Endorsers)
	}
	if after.State == Released && before.State == Pending && fn == "Expire" && tx.Now < before.HoldUntil {
		t.Fatal("released before HoldUntil")
	}
	if after.State == Recalled && before.State == Pending {
		t.Fatal("recalled without a recall request or dispute")
	}
	if len(after.Events) != len(before.Events)+1 {
		t.Fatalf("%s must append exactly one audit event", fn)
	}
}

package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/Karman580/TwoKey/chaincode/credit"
)

type org struct {
	name string
	sk   ed25519.PrivateKey
}

func setup(t *testing.T) (*Ledger, map[string]org) {
	t.Helper()
	orgs := map[string]org{}
	keys := map[string]ed25519.PublicKey{}
	for _, n := range []string{"PayerBank", "PayeeBank", credit.NPCIObserver} {
		pub, sk, _ := ed25519.GenerateKey(nil)
		orgs[n] = org{n, sk}
		keys[n] = pub
	}
	return newLedger(keys), orgs
}

func proposal(id, fn, creator, args string, signers ...org) Proposal {
	p := Proposal{TxID: id, Fn: fn, Creator: creator, Args: args}
	for _, s := range signers {
		p.Endorsements = append(p.Endorsements, Endorsement{s.name, base64.StdEncoding.EncodeToString(ed25519.Sign(s.sk, p.SigningBytes()))})
	}
	return p
}

func holdArgs() string {
	b, _ := json.Marshal(credit.HoldRequest{TxnRef: "RRN9", AmountPaise: 4_000_000, PayerBank: "PayerBank",
		PayeeBank: "PayeeBank", HashedPayer: "a", HashedPayee: "b", Tier: credit.High,
		ReasonCodes: []string{"NEW_ACCT", "HIGH_FAN_IN"}, HoldSeconds: credit.MaxHoldSeconds})
	return string(b)
}

func code(err error) string {
	var te *TxError
	if errors.As(err, &te) {
		return te.Code
	}
	return ""
}

func TestEndToEndAndAttacks(t *testing.T) {
	l, o := setup(t)
	payer, payee, npci := o["PayerBank"], o["PayeeBank"], o[credit.NPCIObserver]
	ref := `{"txnRef":"RRN9"}`

	if _, _, err := l.Submit(proposal("h1", "CreateHold", "PayerBank", holdArgs(), payer), 100); code(err) != "ENDORSEMENT_POLICY_FAILURE" {
		t.Fatalf("hold with one endorser: %v", err)
	}
	if _, _, err := l.Submit(proposal("h1", "CreateHold", "PayerBank", holdArgs(), payer, payee), 100); err != nil {
		t.Fatal(err)
	}
	if c, blk, err := l.Submit(proposal("h1b", "CreateHold", "PayerBank", holdArgs(), payer, payee), 101); err != nil || blk != nil || c.TxnRef != "RRN9" {
		t.Fatalf("idempotent CreateHold: %v %v", blk, err)
	}

	// Colluding payee bank tries to release alone.
	if _, _, err := l.Submit(proposal("x1", "Expire", "PayeeBank", ref, payee), 200); code(err) != "ENDORSEMENT_POLICY_FAILURE" {
		t.Fatalf("unilateral release: %v", err)
	}
	// Payee bank forges the payer bank's signature with its own key.
	forged := proposal("x2", "ApproveRelease", "PayeeBank", ref, payee)
	forged.Endorsements = append(forged.Endorsements, Endorsement{"PayerBank", forged.Endorsements[0].Sig})
	if _, _, err := l.Submit(forged, 200); code(err) != "INVALID_SIGNATURE" {
		t.Fatalf("forged endorsement: %v", err)
	}
	// Creator who did not sign.
	if _, _, err := l.Submit(proposal("x3", "ApproveRelease", "PayerBank", ref, payee, npci), 200); code(err) != "INVALID_SIGNATURE" {
		t.Fatalf("unsigned creator: %v", err)
	}

	// Fraud path: recall, replay, confirm.
	rr := proposal("r1", "RequestRecall", "PayerBank", `{"txnRef":"RRN9","arg":"1930-CMP-77"}`, payer, payee)
	if c, _, err := l.Submit(rr, 300); err != nil || c.State != credit.RecallRequested {
		t.Fatalf("recall: %v", err)
	}
	if _, _, err := l.Submit(rr, 301); code(err) != "REPLAY" {
		t.Fatalf("replay: %v", err)
	}
	if c, _, err := l.Submit(proposal("r2", "ConfirmRecall", "PayeeBank", ref, payee, payer), 400); err != nil || c.State != credit.Recalled {
		t.Fatalf("confirm: %v", err)
	}
	if ok, h := l.verifyChain(); !ok || h != 3 {
		t.Fatalf("chain: %v %d", ok, h)
	}
	l.chain[1].At++ // tamper with history
	if ok, _ := l.verifyChain(); ok {
		t.Fatal("tampered chain must not verify")
	}
}

func TestLogReplayRebuildsState(t *testing.T) {
	l, o := setup(t)
	path := filepath.Join(t.TempDir(), "ledger.jsonl")
	f, _ := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	l.logFile = f
	payer, payee := o["PayerBank"], o["PayeeBank"]
	l.Submit(proposal("h1", "CreateHold", "PayerBank", holdArgs(), payer, payee), 100)
	l.Submit(proposal("a1", "ApproveRelease", "PayeeBank", `{"txnRef":"RRN9"}`, payee, payer), 150)
	l.Submit(proposal("a2", "ApproveRelease", "PayerBank", `{"txnRef":"RRN9"}`, payer, payee), 160)
	f.Close()

	l2 := newLedger(l.keys)
	if err := l2.replay(path); err != nil {
		t.Fatal(err)
	}
	if l2.credits["RRN9"].State != credit.Released || l2.chain[2].Hash != l.chain[2].Hash {
		t.Fatal("replay did not reproduce state and hashes")
	}
}

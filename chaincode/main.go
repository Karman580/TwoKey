// Command chaincode is the ConditionalCredit contract for Drunix (Hyperledger
// Fabric 2.5 compatible). All business rules live in package credit; this file
// maps them onto the Fabric stub: client identity, transaction time, world
// state, private data and state-based endorsement.
//
// Organisation names are MSP IDs without the "MSP" suffix, so PayerBankMSP is
// "PayerBank". Endorsement is enforced by the network, not inside this code:
// each credit key carries a state-based policy AND(payer peer, payee peer),
// extended with the NPCI observer once the credit is disputed.
package main

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/Karman580/TwoKey/chaincode/credit"
	"github.com/hyperledger/fabric-chaincode-go/pkg/statebased"
	"github.com/hyperledger/fabric-contract-api-go/contractapi"
)

// PrivateCollection holds VPAs and KYC references, shared only by the two banks.
const PrivateCollection = "payerPayeeCollection"

// Contract exposes the ConditionalCredit functions.
type Contract struct {
	contractapi.Contract
}

func org(ctx contractapi.TransactionContextInterface) (string, error) {
	msp, err := ctx.GetClientIdentity().GetMSPID()
	if err != nil {
		return "", err
	}
	return strings.TrimSuffix(msp, "MSP"), nil
}

func txFor(ctx contractapi.TransactionContextInterface, c *credit.ConditionalCredit) (credit.Tx, error) {
	creator, err := org(ctx)
	if err != nil {
		return credit.Tx{}, err
	}
	ts, err := ctx.GetStub().GetTxTimestamp()
	if err != nil {
		return credit.Tx{}, err
	}
	// The network has already validated the key's endorsement policy before a
	// write commits, so the endorsers are the policy's required organisations.
	return credit.Tx{ID: ctx.GetStub().GetTxID(), Creator: creator,
		Endorsers: credit.RequiredEndorsers(c), Now: ts.GetSeconds()}, nil
}

func setKeyPolicy(ctx contractapi.TransactionContextInterface, c *credit.ConditionalCredit) error {
	ep, err := statebased.NewStateEP(nil)
	if err != nil {
		return err
	}
	var msps []string
	for _, o := range credit.RequiredEndorsers(c) {
		msps = append(msps, o+"MSP")
	}
	if err := ep.AddOrgs(statebased.RoleTypePeer, msps...); err != nil {
		return err
	}
	policy, err := ep.Policy()
	if err != nil {
		return err
	}
	return ctx.GetStub().SetStateValidationParameter(c.TxnRef, policy)
}

func put(ctx contractapi.TransactionContextInterface, c *credit.ConditionalCredit) error {
	b, err := json.Marshal(c)
	if err != nil {
		return err
	}
	if err := ctx.GetStub().PutState(c.TxnRef, b); err != nil {
		return err
	}
	return ctx.GetStub().SetEvent("CreditChanged", b)
}

// GetCredit returns the shared view of a credit.
func (Contract) GetCredit(ctx contractapi.TransactionContextInterface, txnRef string) (*credit.ConditionalCredit, error) {
	b, err := ctx.GetStub().GetState(txnRef)
	if err != nil {
		return nil, err
	}
	if b == nil {
		return nil, fmt.Errorf("credit %s not found", txnRef)
	}
	var c credit.ConditionalCredit
	return &c, json.Unmarshal(b, &c)
}

// CreateHold records a PENDING credit. Cleartext VPAs, names and KYC
// references may be passed in the transient field "private"; they go to the
// private data collection and never to the shared channel.
func (s Contract) CreateHold(ctx contractapi.TransactionContextInterface, reqJSON string) (*credit.ConditionalCredit, error) {
	var req credit.HoldRequest
	if err := json.Unmarshal([]byte(reqJSON), &req); err != nil {
		return nil, fmt.Errorf("bad request: %w", err)
	}
	stored, err := ctx.GetStub().GetState(req.TxnRef)
	if err != nil {
		return nil, err
	}
	if stored != nil {
		var existing credit.ConditionalCredit
		if err := json.Unmarshal(stored, &existing); err != nil {
			return nil, err
		}
		if credit.SameHold(&existing, req) {
			return &existing, nil // idempotent retry
		}
		return nil, fmt.Errorf("txnRef %s already holds a different credit", req.TxnRef)
	}
	tx, err := txFor(ctx, &credit.ConditionalCredit{PayerBank: req.PayerBank, PayeeBank: req.PayeeBank})
	if err != nil {
		return nil, err
	}
	c, err := credit.NewHold(req, tx)
	if err != nil {
		return nil, err
	}
	transient, err := ctx.GetStub().GetTransient()
	if err != nil {
		return nil, err
	}
	if p, ok := transient["private"]; ok {
		if err := ctx.GetStub().PutPrivateData(PrivateCollection, c.TxnRef, p); err != nil {
			return nil, err
		}
	}
	if err := put(ctx, c); err != nil {
		return nil, err
	}
	return c, setKeyPolicy(ctx, c)
}

func (s Contract) apply(ctx contractapi.TransactionContextInterface, fn, txnRef, arg string) (*credit.ConditionalCredit, error) {
	c, err := s.GetCredit(ctx, txnRef)
	if err != nil {
		return nil, err
	}
	tx, err := txFor(ctx, c)
	if err != nil {
		return nil, err
	}
	n, err := credit.Apply(c, fn, arg, tx)
	if err != nil {
		return nil, err
	}
	if err := put(ctx, n); err != nil {
		return nil, err
	}
	if n.State != c.State && (n.State == credit.Disputed || c.State == credit.Disputed) {
		return n, setKeyPolicy(ctx, n)
	}
	return n, nil
}

// RequestRecall is submitted by the payer bank after a 1930 report.
func (s Contract) RequestRecall(ctx contractapi.TransactionContextInterface, txnRef, reportRef string) (*credit.ConditionalCredit, error) {
	return s.apply(ctx, "RequestRecall", txnRef, reportRef)
}

// ConfirmRecall is submitted by the payee bank; funds return to the payer.
func (s Contract) ConfirmRecall(ctx contractapi.TransactionContextInterface, txnRef string) (*credit.ConditionalCredit, error) {
	return s.apply(ctx, "ConfirmRecall", txnRef, "")
}

// ApproveRelease records one bank's consent; the second consent releases.
func (s Contract) ApproveRelease(ctx contractapi.TransactionContextInterface, txnRef string) (*credit.ConditionalCredit, error) {
	return s.apply(ctx, "ApproveRelease", txnRef, "")
}

// Expire releases a PENDING credit once its hold has ended.
func (s Contract) Expire(ctx contractapi.TransactionContextInterface, txnRef string) (*credit.ConditionalCredit, error) {
	return s.apply(ctx, "Expire", txnRef, "")
}

// RaiseDispute freezes the credit until the NPCI observer rules.
func (s Contract) RaiseDispute(ctx contractapi.TransactionContextInterface, txnRef, reason string) (*credit.ConditionalCredit, error) {
	return s.apply(ctx, "RaiseDispute", txnRef, reason)
}

// ResolveDispute is submitted by the NPCI observer with RELEASE or RECALL.
func (s Contract) ResolveDispute(ctx contractapi.TransactionContextInterface, txnRef, outcome string) (*credit.ConditionalCredit, error) {
	return s.apply(ctx, "ResolveDispute", txnRef, outcome)
}

func main() {
	cc, err := contractapi.NewChaincode(&Contract{})
	if err != nil {
		panic(err)
	}
	if err := cc.Start(); err != nil {
		panic(err)
	}
}

package main

import (
	"encoding/json"
	"fmt"
	"os"
)

// Deterministic offline compatibility checks never read production credentials.
type fixtureStep struct {
	Now     float64   `json:"now"`
	Service string    `json:"service"`
	Data    *Snapshot `json:"data"`
	Records *[]Record `json:"records"`
	Fail    string    `json:"fail"`
}
type fixtureData struct {
	Config Config        `json:"config"`
	Steps  []fixtureStep `json:"steps"`
	Now    float64       `json:"now"`
}

func runFixture(path string) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	fixture := fixtureData{Config: defaultConfig()}
	if err = json.Unmarshal(raw, &fixture); err != nil {
		return err
	}
	c := newCollector(fixture.Config)
	for _, step := range fixture.Steps {
		if step.Data != nil {
			found := false
			for _, s := range fixture.Config.Services {
				if s.Name == step.Service {
					found = true
					if err := c.update(s, *step.Data, step.Now); err != nil {
						return err
					}
				}
			}
			if !found {
				return fmt.Errorf("unknown fixture service")
			}
		}
		if step.Fail != "" {
			state := c.states[step.Fail]
			if state == nil {
				state = newState()
				c.states[step.Fail] = state
			}
			state.Up = false
			state.Errors++
		}
		if step.Records != nil {
			c.tcpUpdate(*step.Records, step.Now)
		}
	}
	result := struct {
		Metrics     string             `json:"metrics"`
		Connections ConnectionSnapshot `json:"connections"`
	}{string(c.render(fixture.Now)), c.connections(fixture.Now)}
	return json.NewEncoder(os.Stdout).Encode(result)
}

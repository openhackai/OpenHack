import { useState } from "react";
import { Button } from "../components/controls";
import { ArrowUpRight, ChevronRight } from "../components/nucleo-icons";
import { ProLogo } from "../components/ProLogo";
import "./pro.css";

const features = [
  {
    title: "Business impact",
    description:
      "See what matters across your organization, with prioritized actions and security briefings.",
    image: "business-impact.png",
    alt: "OpenHack organization briefing showing recommended actions, security metrics, and backlog trends for the ACME demo organization.",
    docs: "https://openhack.com/docs/platform/briefings",
  },
  {
    title: "Finding triage",
    description:
      "Bring evidence, ownership, and remediation status together so your team can focus on the right findings.",
    image: "findings.png",
    alt: "OpenHack findings workspace with a selected vulnerability, severity, evidence, and assignee using fictional ACME data.",
    docs: "https://openhack.com/docs/platform/findings",
  },
  {
    title: "Automatic fixes",
    description:
      "Move from findings to fixes in your pull requests, with AI Autofix and changes your team can review.",
    image: "autofix.png",
    alt: "OpenHack review summary on a demo GitHub pull request with priority counts and AI Autofix actions.",
    docs: "https://openhack.com/docs/platform/github-reviews",
  },
  {
    title: "Slack integration",
    description:
      "Bring security into your team's conversations. Ask OpenHack questions in Slack and get scan updates in your connected channels.",
    image: "slack.png",
    alt: "OpenHack Slack channel setup showing how to connect the fictional ACME organization and configure scan notifications.",
    docs: "https://openhack.com/docs/platform/integrations/slack",
  },
] as const;

export function ProPage() {
  const [selected, setSelected] = useState(0);
  const feature = features[selected]!;
  return (
    <section className="page pro-page">
      <div className="pagebar">
        <div className="page-title">
          <h1>OpenHack Pro</h1>
        </div>
      </div>
      <div className="pro-scroll">
        <div className="pro-content">
          <header className="pro-hero">
            <div className="pro-brand">
              <ProLogo size={30} />
              <span>
                OpenHack <strong>PRO</strong>
              </span>
            </div>
            <h2>Looking to go enterprise?</h2>
            <p className="pro-lead">
              OpenHack Pro lets you assess true business impact.
            </p>
            <p className="pro-description">
              It's an always-on AI security engineer that assesses true business
              impact, triages findings, and automatically creates fixes.
            </p>
            <div className="pro-actions">
              <a
                className="button pro-cta"
                href="https://openhack.com/demo"
                target="_blank"
                rel="noopener noreferrer"
              >
                Book a demo <ArrowUpRight size={16} />
              </a>
              <a
                className="button"
                href="https://openhack.com/docs"
                target="_blank"
                rel="noopener noreferrer"
              >
                Read the docs
              </a>
            </div>
          </header>
          <section
            className="pro-showcase"
            aria-label="Explore OpenHack Pro features"
          >
            <div
              className="pro-feature-buttons"
              role="group"
              aria-label="Product preview"
            >
              {features.map((item, index) => (
                <Button
                  key={item.title}
                  aria-pressed={selected === index}
                  aria-controls="pro-preview"
                  onClick={() => setSelected(index)}
                >
                  <span className="pro-feature-number">0{index + 1}</span>
                  {item.title}
                </Button>
              ))}
            </div>
            <figure id="pro-preview" className="pro-preview">
              <div className="pro-preview-heading">
                <div>
                  <h3>{feature.title}</h3>
                  <p>{feature.description}</p>
                </div>
                <a
                  href={feature.docs}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={`Read the ${feature.title.toLowerCase()} guide`}
                >
                  <ChevronRight size={18} />
                </a>
              </div>
              <a
                className="pro-image-link"
                href={feature.docs}
                target="_blank"
                rel="noopener noreferrer"
              >
                <img src={`/pro/${feature.image}`} alt={feature.alt} />
              </a>
              <figcaption>
                Product screenshots from the OpenHack docs. Fictional demo data.
              </figcaption>
            </figure>
          </section>
        </div>
      </div>
    </section>
  );
}

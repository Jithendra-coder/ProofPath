import { Icon } from "./icons";

export function ProjectArt({
  kind = "folio",
  compact = false,
}: {
  kind?: string;
  compact?: boolean;
}) {
  return (
    <div
      className={`project-art art-${kind} ${compact ? "art-compact" : ""}`}
      aria-hidden="true"
    >
      {kind === "folio" ? (
        <>
          <div className="art-wordmark">
            folio<span>®</span>
            <small>
              A little clarity.
              <br />A lot of possibility.
            </small>
          </div>
          <div className="finance-phone">
            <div className="phone-top">
              9:41 <span>•••</span>
            </div>
            <span className="phone-greeting">Good morning, Alex</span>
            <strong>
              $8,250<span>.00</span>
            </strong>
            <div className="phone-chart">
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
            </div>
            <div className="phone-pill">
              <span>↗</span> Your money, moving forward.
            </div>
            <div className="phone-bottom">
              <span>
                ↗<small>Income</small>
              </span>
              <span>
                ↙<small>Expenses</small>
              </span>
              <span>
                ◈<small>Savings</small>
              </span>
            </div>
          </div>
          <div className="art-orbit" />
        </>
      ) : kind === "forma" ? (
        <>
          <div className="forma-heading">
            forma<span>Design in good company.</span>
          </div>
          <div className="component-board">
            <div className="component-swatches">
              <i />
              <i />
              <i />
              <i />
            </div>
            <div className="component-button">
              Let’s make something <Icon name="arrow" size={13} />
            </div>
            <div className="component-row">
              <span>Aa</span>
              <div>
                <b>Thoughtful by design.</b>
                <small>Made to fit. Built to grow.</small>
              </div>
            </div>
            <div className="component-progress">
              <i />
            </div>
            <div className="component-tags">
              <span>Components</span>
              <span>Foundations</span>
            </div>
          </div>
        </>
      ) : kind === "waypoint" ? (
        <>
          <div className="waypoint-sun" />
          <div className="waypoint-hill hill-back" />
          <div className="waypoint-hill" />
          <div className="waypoint-title">
            Find your
            <br />
            <i>somewhere.</i>
            <span>WAYPOINT ↗</span>
          </div>
        </>
      ) : (
        <>
          <div className="generic-art">
            <Icon name="folder" size={48} />
            <span>
              A work in progress.
              <br />A story worth telling.
            </span>
          </div>
        </>
      )}
    </div>
  );
}

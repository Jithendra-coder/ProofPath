import Link from "next/link";
import { AppHeader } from "./app-header";
import { BrandMark, Icon } from "./icons";
import { ProjectArt } from "./project-art";

const steps = [
  {
    number: "01",
    icon: "folder" as const,
    title: "Show what you can do.",
    text: "Give your projects a home. Share the problem, your process, and the difference your work made.",
  },
  {
    number: "02",
    icon: "award" as const,
    title: "Put proof behind your skills.",
    text: "Connect your skills to real work. Build a fuller picture of your abilities, one project at a time.",
  },
  {
    number: "03",
    icon: "briefcase" as const,
    title: "Find your next chapter.",
    text: "Discover teams that value what you bring. Share the right work with the right opportunity.",
  },
];

export function HomePage() {
  return (
    <div className="marketing">
      <AppHeader section="public" activePath="/" />
      <main id="main-content">
        <section className="landing-hero page-width">
          <div className="landing-copy">
            <div className="eyebrow">
              <span className="tiny-dot" /> YOUR POTENTIAL. MADE VISIBLE.
            </div>
            <h1>
              You’re more
              <br />
              than a résumé.
              <br />
              <em>Show your proof.</em>
            </h1>
            <p>
              A home for your skills, your work, and everything you’re becoming.
              Let what you can do open the next door.
            </p>
            <div className="landing-actions">
              <Link className="button primary" href="/signup">
                Build your profile <Icon name="arrow" size={18} />
              </Link>
              <Link className="button plain" href="/explore">
                Explore Proofpath <Icon name="upRight" size={17} />
              </Link>
            </div>
            <div className="hero-reassurance">
              <Icon name="check" size={15} /> Free to start <span>·</span> Your
              work, your story <span>·</span> Always in your control
            </div>
          </div>
          <div
            className="landing-art"
            aria-label="Example portfolio for Alex Morgan"
          >
            <div className="art-coordinate coordinate-top">
              A LITTLE PROOF GOES A LONG WAY.
            </div>
            <div className="profile-preview">
              <div className="preview-topline">
                <BrandMark />
                <span>YOUR WORK SPEAKS VOLUMES.</span>
                <Icon name="upRight" size={15} />
              </div>
              <div className="preview-person">
                <div className="avatar avatar-large">
                  am
                  <span />
                </div>
                <div>
                  <h2>
                    Alex Morgan <Icon name="award" size={19} />
                  </h2>
                  <p>Product designer. Thoughtful problem solver.</p>
                </div>
              </div>
              <p className="preview-bio">
                Turning complex problems into
                <br />
                simple, human experiences.
              </p>
              <div className="preview-tags">
                <span>Product design</span>
                <span>Figma</span>
                <span>Design systems</span>
              </div>
              <div className="preview-work">
                <ProjectArt compact kind="folio" />
                <ProjectArt compact kind="forma" />
              </div>
              <div className="preview-footer">
                <span>
                  <span className="tiny-dot" /> Open to the right opportunity
                </span>
                <span>View profile ↗</span>
              </div>
            </div>
            <div className="art-coordinate coordinate-bottom">
              BUILT ON ABILITY. OPEN TO POSSIBILITY. ↗
            </div>
          </div>
        </section>
        <section className="belief-strip">
          <div className="page-width">
            <p>
              Good work deserves
              <br />
              <strong>to be seen.</strong>
            </p>
            <div>
              <Icon name="folder" />
              <span>Projects with a story</span>
            </div>
            <div>
              <Icon name="award" />
              <span>Skills with substance</span>
            </div>
            <div>
              <Icon name="users" />
              <span>Connections with purpose</span>
            </div>
          </div>
        </section>
        <section className="landing-section page-width" id="how-it-works">
          <div className="section-intro">
            <div>
              <div className="eyebrow">LESS CLAIMING. MORE SHOWING.</div>
              <h2>
                Make your next move
                <br />
                with something <em>real.</em>
              </h2>
            </div>
            <p>
              Your career isn’t a straight line. Proofpath brings the work,
              skills, and experiences that make you, you, into one place.
            </p>
          </div>
          <div className="how-grid">
            {steps.map((step) => (
              <article key={step.number}>
                <div className="step-top">
                  <span className="step-icon">
                    <Icon name={step.icon} size={24} />
                  </span>
                  <span>{step.number}</span>
                </div>
                <h3>{step.title}</h3>
                <p>{step.text}</p>
              </article>
            ))}
          </div>
        </section>
        <section className="spotlight-section page-width" id="candidates">
          <div className="spotlight-visual">
            <ProjectArt kind="forma" />
            <div className="spotlight-caption">
              <span>THE WORK BEHIND THE WORDS</span>
              <b>Built with intention. Shared with confidence.</b>
            </div>
          </div>
          <div className="spotlight-copy">
            <div className="eyebrow">A PROFILE THAT FEELS LIKE YOU</div>
            <h2>
              Your experience.
              <br />
              Your perspective.
              <br />
              <em>Your advantage.</em>
            </h2>
            <p>
              A title tells part of the story. Your work tells the rest. Bring
              your best projects together in a professional profile that makes
              your strengths easy to understand.
            </p>
            <ul>
              <li>
                <Icon name="check" /> Put your projects and process in the
                spotlight
              </li>
              <li>
                <Icon name="check" /> Connect each skill to the work behind it
              </li>
              <li>
                <Icon name="check" /> Choose what to share, and when
              </li>
            </ul>
            <Link className="text-link" href="/explore/profile">
              See an example profile <Icon name="arrow" size={18} />
            </Link>
          </div>
        </section>
        <section className="teams-section page-width" id="teams">
          <div>
            <div className="eyebrow">FOR TEAMS THAT SEE POTENTIAL</div>
            <h2>
              The right person
              <br />
              is more than the <em>right keywords.</em>
            </h2>
            <p>
              Get closer to the work behind an application. Discover skills,
              understand the thinking, and start a better conversation.
            </p>
            <Link className="button primary" href="/signup?purpose=employer">
              Build your hiring workspace <Icon name="arrow" size={18} />
            </Link>
          </div>
          <div className="team-proof">
            <div className="team-proof-title">
              <Icon name="users" />
              <span>A clearer picture of potential.</span>
            </div>
            {[
              {
                initials: "am",
                name: "Alex Morgan",
                role: "Product Designer",
                skill: "Design systems",
                count: "3 projects",
              },
              {
                initials: "jl",
                name: "Jamie Lee",
                role: "Frontend Engineer",
                skill: "React",
                count: "4 projects",
              },
              {
                initials: "sk",
                name: "Sam Kim",
                role: "UX Researcher",
                skill: "User research",
                count: "2 projects",
              },
            ].map((person, i) => (
              <div className="talent-row" key={person.name}>
                <span className={`avatar avatar-tone-${i}`}>
                  {person.initials}
                </span>
                <div>
                  <b>{person.name}</b>
                  <span>{person.role}</span>
                </div>
                <div className="talent-skill">
                  <span>{person.skill}</span>
                  <small>{person.count}</small>
                </div>
                <Icon name="upRight" size={17} />
              </div>
            ))}
            <p>Example profiles · People make the hiring decisions.</p>
          </div>
        </section>
        <section className="faq-block page-width" id="faq">
          <div>
            <div className="eyebrow">A FEW GOOD QUESTIONS</div>
            <h2>
              Let’s clear
              <br />
              things <em>up.</em>
            </h2>
          </div>
          <div>
            {[
              [
                "Who is Proofpath for?",
                "Anyone who wants their work to speak for them. Whether you’re finding your first role, changing direction, or building on years of experience, your projects and skills have a place here.",
              ],
              [
                "Do I need a finished portfolio to get started?",
                "No. Start with one project, a few skills, or simply your story. Keep your work private while you build, and share it when you’re ready.",
              ],
              [
                "Is creating a profile free?",
                "Yes. You can create a profile, add projects, and publish your work for free. Advanced portfolio customization depends on account access; no payment is required to get started.",
              ],
              [
                "Who can see my work?",
                "You choose. Projects begin as private drafts. Publishing your profile, making projects public, and opting into employer discovery are separate choices that you control.",
              ],
              [
                "Are all skills verified?",
                "No. Skills you add yourself are self-reported. Project evidence and completed assessments provide additional context, and verification is shown only when it is supported.",
              ],
            ].map(([question, answer]) => (
              <details key={question}>
                <summary>
                  {question}
                  <Icon name="plus" size={18} />
                </summary>
                <p>{answer}</p>
              </details>
            ))}
          </div>
        </section>
        <section className="closing-section page-width">
          <div className="eyebrow">YOUR NEXT CHAPTER STARTS WITH YOU</div>
          <h2>
            Good work.
            <br />
            <em>New possibilities.</em>
          </h2>
          <p>You’ve got something to offer. Give it a place to grow.</p>
          <Link className="button primary" href="/signup">
            Start your Proofpath <Icon name="arrow" size={18} />
          </Link>
        </section>
      </main>
      <footer className="landing-footer page-width">
        <Link href="/" className="app-brand">
          <BrandMark />
          proofpath<span className="brand-period">.</span>
        </Link>
        <span>Built around what you can do.</span>
        <nav aria-label="Footer">
          <Link href="/jobs">Opportunities</Link>
          <Link href="/explore">Explore the platform</Link>
          <Link href="/login">Sign in</Link>
        </nav>
        <small>© 2026 Proofpath</small>
      </footer>
    </div>
  );
}

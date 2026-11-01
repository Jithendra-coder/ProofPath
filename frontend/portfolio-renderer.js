(() => {
  const sections = new Set(["roles", "skills", "projects"]);
  const templates = new Set(["basic", "editorial", "engineer", "case-study", "compact-resume"]);
  const themes = new Set(["proofpath", "coral", "teal", "ink"]);

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function chips(title, values) {
    const section = element("section", "portfolio-section");
    section.dataset.section = title === "Skills" ? "skills" : "roles";
    section.append(element("h2", "portfolio-section-title", title));
    const list = element("ul", "portfolio-chips");
    for (const value of values) list.append(element("li", "portfolio-chip", value));
    if (values.length) section.append(list);
    else section.append(element("p", "portfolio-empty", `No ${title.toLowerCase()} added yet.`));
    return section;
  }

  function projectsSection(projects) {
    const section = element("section", "portfolio-section portfolio-projects");
    section.dataset.section = "projects";
    section.append(element("h2", "portfolio-section-title", "Selected work"));
    if (!projects.length) {
      section.append(element("p", "portfolio-empty", "No public projects yet."));
      return section;
    }
    const list = element("div", "portfolio-project-list");
    for (const project of projects) {
      const card = element("article", "portfolio-project");
      card.append(element("h3", "portfolio-project-title", project.name));
      for (const [label, value] of [["Problem", project.problem_statement], ["Contribution", project.contribution]]) {
        if (!value) continue;
        const paragraph = element("p", "portfolio-project-copy");
        paragraph.append(element("strong", "", `${label}: `), document.createTextNode(value));
        card.append(paragraph);
      }
      if (project.technologies.length) {
        const tech = element("ul", "portfolio-chips");
        for (const value of project.technologies) tech.append(element("li", "portfolio-chip", value));
        card.append(tech);
      }
      for (const url of project.source_links) {
        const link = element("a", "portfolio-project-link", "View public repository");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        card.append(link);
      }
      list.append(card);
    }
    section.append(list);
    return section;
  }

  function makeSection(key, portfolio) {
    if (key === "roles") return chips("Roles", portfolio.target_roles);
    if (key === "skills") return chips("Skills", portfolio.skills);
    return projectsSection(portfolio.projects);
  }

  function render(root, portfolio, design) {
    const template = templates.has(design?.template_key) ? design.template_key : "basic";
    const theme = themes.has(design?.theme_key) ? design.theme_key : "proofpath";
    const order = Array.isArray(design?.sections) ? design.sections.filter((key) => sections.has(key)) : [];
    const visibleSections = [...new Set(order)].length ? [...new Set(order)] : ["roles", "skills", "projects"];
    root.replaceChildren();
    root.className = `portfolio-template portfolio-template--${template} portfolio-theme--${theme}`;

    const intro = element("header", "portfolio-intro");
    intro.append(element("p", "portfolio-eyebrow", "Proofpath portfolio"));
    intro.append(element("h1", "portfolio-name", portfolio.display_name));

    if (template === "engineer") {
      const layout = element("div", "portfolio-engineer-layout");
      const sidebar = element("aside", "portfolio-engineer-sidebar");
      sidebar.setAttribute("aria-label", "Candidate profile details");
      const content = element("div", "portfolio-engineer-content");
      content.append(intro);
      for (const key of visibleSections) {
        const block = makeSection(key, portfolio);
        (key === "projects" ? content : sidebar).append(block);
      }
      layout.append(sidebar, content);
      root.append(layout);
      return;
    }

    if (template === "case-study") {
      root.append(intro);
      const projectIndex = visibleSections.indexOf("projects");
      if (projectIndex !== -1) root.append(makeSection("projects", portfolio));
      const details = element("div", "portfolio-case-study-details");
      for (const key of visibleSections) if (key !== "projects") details.append(makeSection(key, portfolio));
      if (details.childElementCount) root.append(details);
      return;
    }

    root.append(intro);
    const content = element("div", "portfolio-sections");
    for (const key of visibleSections) content.append(makeSection(key, portfolio));
    root.append(content);
  }

  window.ProofpathPortfolioRenderer = { render };
})();

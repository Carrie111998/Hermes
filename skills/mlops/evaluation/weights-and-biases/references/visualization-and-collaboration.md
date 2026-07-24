# Visualization, Reports, Teams, Pricing

Turning logged runs into charts and shareable narrative, and the account-level
facts (team setup, plan limits) that decide where runs can live.

## Custom charts

```python
# Log custom visualizations
import matplotlib.pyplot as plt

fig, ax = plt.subplots()
ax.plot(x, y)
wandb.log({"custom_plot": wandb.Image(fig)})

# Log confusion matrix
wandb.log({"conf_mat": wandb.plot.confusion_matrix(
    probs=None,
    y_true=ground_truth,
    preds=predictions,
    class_names=class_names
)})
```

`wandb.Image(fig)` uploads a static raster — the chart is frozen at log time.
`wandb.plot.*` helpers upload the underlying data, so the panel stays interactive
and comparable across runs. Prefer `wandb.plot.*` when a built-in exists.

## Reports

Create shareable reports in the W&B UI:
- Combine runs, charts, and text
- Markdown support
- Embeddable visualizations
- Team collaboration

Reports snapshot the query, not the data, so a report keeps updating as new runs
land in the project it points at.

## Sharing runs

```python
# Runs are automatically shareable via URL
run = wandb.init(project="team-project")
print(f"Share this URL: {run.url}")
```

A run URL inherits the project's visibility: in a public project the link is
world-readable. Check project visibility before pasting a run URL outside the team.

## Team projects

- Create team account at wandb.ai
- Add team members
- Set project visibility (private/public)
- Use team-level artifacts and model registry

## Pricing

- **Free**: unlimited public projects, 100GB storage
- **Academic**: free for students/researchers
- **Teams**: $50/seat/month, private projects, unlimited storage
- **Enterprise**: custom pricing, on-prem options

Plan tiers and quotas change; confirm current limits at https://wandb.ai/site/pricing
before committing to a storage-heavy artifact strategy.

## Resources

- Documentation: https://docs.wandb.ai
- GitHub: https://github.com/wandb/wandb (10.5k+ stars)
- Examples: https://github.com/wandb/examples
- Community: https://wandb.ai/community
- Discord: https://wandb.me/discord
